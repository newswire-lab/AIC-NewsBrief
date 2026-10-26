"""后台自动更新：定时执行「采集 -> 聚类 -> 打分」，让界面无需手动刷新。

为什么需要这个模块
------------------
热点新闻产品的核心是"新"。库里若没有近 24 小时的数据，
:func:`src.credibility.time_factor` 的时间因子会按 ``exp(-age/72h)`` 衰减，
界面上**所有**事件的分数都会掉到个位数。实测把数据放置 5 天后，
原本 40 分的事件只剩 7 分左右 —— 这不是模型的问题，是数据陈旧的问题。

因此本模块把"定期采集"做成常驻能力，而不是依赖人记得去敲
``python -m src.cli pipeline``。界面只要读库，数据由本模块在后台保活。

设计要点
--------
1. **守护线程 + 可中断等待**：用 ``Event.wait()`` 而不是 ``time.sleep()``，
   :meth:`AutoUpdater.stop` 能立刻唤醒线程退出，进程结束不留悬挂线程。

2. **每轮新建 SQLite 连接**：``sqlite3`` 的连接默认不可跨线程复用。
   每轮 ``connect()`` / ``close()``，不在长时间存活的对象里持有连接。
   搭配 ``store.connect()`` 的 ``timeout=30``，界面并发读不会被写操作噎住。

3. **不重入**：``_state["running"]`` 保证同一时刻只有一轮在跑，
   界面上的"立即更新"连点不会叠加出多条采集流水线。

4. **失败不致命**：单轮异常只记进 ``last_error`` 并等下一个周期。
   一次网络抖动不该搞死常驻线程，也不该让界面卡住。

5. **状态可读**：:meth:`AutoUpdater.status` 返回锁保护的快照，
   界面直接渲染「最后更新 / 下次更新」，不需要自己去查库算。

6. **启动按需补跑**：若库内数据已超过 ``stale_hours``，启动后立刻跑一轮
   （否则界面一打开就是空的热点列表）；数据还新鲜时先睡一个周期，
   避免每次重启都白跑一次全量采集。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
import threading
import time

from . import cluster, collect, config, credibility, keywords, store, topics
from .models import iso, utcnow

DEFAULT_INTERVAL_MINUTES = 30.0   # 默认采集周期
MIN_INTERVAL_MINUTES = 1.0        # 周期下限：比这更密属于对源站不礼貌，构造时会夹到该值
DEFAULT_STALE_HOURS = 24.0        # 数据超过该时长即认为过期，启动后立即补跑
# 「立即采集」的防护：数据比这更新时，再采一遍没有意义（也挡住重复触发）
DEFAULT_MANUAL_GAP_MINUTES = 5.0
DEFAULT_DELAY = 0.6               # 源之间的间隔，礼貌抓取
DEFAULT_CLUSTER_DAYS = 14         # 聚类回看窗口，与 cli.pipeline 默认值一致


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """解析库里的 UTC ISO 时间串，失败返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class AutoUpdater:
    """定时跑「采集 -> 聚类 -> 打分」的守护线程。

    典型用法::

        updater = AutoUpdater(interval_minutes=30)
        updater.start()          # 数据过期则立刻补跑一轮
        ...
        updater.status()         # 界面读状态
        updater.trigger()        # 用户点"立即更新"
        updater.stop()           # 退出
    """

    def __init__(
        self,
        interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
        stale_hours: float = DEFAULT_STALE_HOURS,
        delay: float = DEFAULT_DELAY,
        days: int = DEFAULT_CLUSTER_DAYS,
        tau: float = credibility.TAU_HOURS,
        manual_gap_minutes: float = DEFAULT_MANUAL_GAP_MINUTES,
        verbose: bool = False,
    ) -> None:
        # 注意：下限在构造时一次性夹紧。运行中直接改 interval_minutes 不会被夹
        # （测试时用得上），但也不会缩短**正在进行**的那次等待。
        self.interval_minutes = max(MIN_INTERVAL_MINUTES, float(interval_minutes))
        self.stale_hours = float(stale_hours)
        self.delay = float(delay)
        self.days = int(days)
        self.tau = float(tau)
        self.manual_gap_minutes = max(0.0, float(manual_gap_minutes))
        self.verbose = bool(verbose)

        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()    # 置位 = 有人要求立刻跑一轮
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = self._blank_state()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @staticmethod
    def _blank_state() -> Dict[str, Any]:
        return {
            "enabled": False,
            "running": False,
            "runs": 0,
            "fails": 0,
            "last_reason": None,
            "last_start": None,
            "last_finish": None,
            "last_elapsed": None,
            "last_ok": None,
            "last_summary": None,
            "last_error": None,
            "next_run": None,
        }

    def status(self, latest_data_time: Optional[str] = None) -> Dict[str, Any]:
        """返回状态快照（含库内最新数据时间与是否过期）。

        :param latest_data_time: 调用方已知的"库内最新采集时间"。
            传 None 时本方法自己去查一次库（一次 ``MAX(fetched_at)`` 很快）。
        """
        if latest_data_time is None:
            latest_data_time = self.latest_data_time()
        with self._lock:
            snap = dict(self._state)
            snap["last_summary"] = (
                None if self._state["last_summary"] is None
                else dict(self._state["last_summary"])
            )
        snap["interval_minutes"] = self.interval_minutes
        snap["stale_hours"] = self.stale_hours
        snap["manual_gap_minutes"] = self.manual_gap_minutes
        snap["latest_data_time"] = latest_data_time
        age = self.data_age_hours(latest_data_time)
        snap["data_age_hours"] = age
        snap["data_stale"] = (age is None) or (age > self.stale_hours)
        return snap

    @staticmethod
    def latest_data_time() -> Optional[str]:
        """库内最新采集时间。空库或异常返回 None。"""
        try:
            conn = store.connect()
            try:
                row = conn.execute("SELECT MAX(fetched_at) AS m FROM articles").fetchone()
                return row["m"] if row else None
            finally:
                conn.close()
        except Exception:
            return None

    @staticmethod
    def data_age_hours(latest_data_time: Optional[str]) -> Optional[float]:
        """距"库内最新采集时间"的小时数。None 表示无法判定。"""
        dt = _parse_iso(latest_data_time)
        if dt is None:
            return None
        return max(0.0, (utcnow() - dt).total_seconds() / 3600.0)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """启动后台线程。已在运行则返回 False。"""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._wake.clear()
        with self._lock:
            self._state["enabled"] = True
        self._thread = threading.Thread(
            target=self._loop, name="newsbrief-autoupdater", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """请求停止并等待线程退出。

        **曾有的 bug**：原来不管 join 是否超时都无条件 `self._thread = None`。
        而单轮采集是「29 个源串行 + 每源 delay + 网络」，远超 timeout=5s，
        所以 join 几乎必然超时 —— 于是句柄被丢掉，但线程还在跑：
        ① `is_alive()` 返回 False，`trigger()` 会报「线程未运行」；
        ② 若再调 `start()`，会起出第二条 updater 线程，
           两条线程同时写 articles/source_runs → `database is locked`
           与 `_state` 错乱。
        现在 join 后若线程仍存活就**保留句柄**，让上层知道它还在收尾。
        """
        with self._lock:
            self._state["enabled"] = False
            self._state["next_run"] = None
        self._stop.set()
        self._wake.set()          # 打断等待，让线程立刻看到停止标志
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            # 还在跑（多半正卡在某个源的网络请求上）：保留句柄，
            # start() 里的 is_alive() 判断会拦住重复启动。
            with self._lock:
                self._state["stopping"] = True
            return
        self._thread = None

    def trigger(self, force: bool = False) -> bool:
        """请求立刻跑一轮（非阻塞）。线程未启动或"没必要"时返回 False。

        默认会先问 :meth:`can_trigger`：数据还很新鲜时直接拒绝。
        这不是多此一举 —— 界面的"立即采集"按钮存在被重复触发的可能
        （实测 Gradio 前端在 SSE 流反复中断后回重放 click 事件），
        而即使不考虑误触，数据 1 分钟前刚采过，再跑一遍 29 个源也纯属浪费。
        需要无条件触发时传 ``force=True``（自动化脚本用）。
        """
        if self._thread is None or not self._thread.is_alive():
            return False
        if not force and not self.can_trigger():
            return False
        self._wake.set()
        return True

    def can_trigger(self) -> bool:
        """现在手动触发一轮采集是否有意义。

        判据用**数据年龄**而不是"上次运行时间"，这样两类情况都能挡住：
        服务刚启动（此前没跑过）与刚跑完一轮。
        """
        with self._lock:
            if self._state["running"]:
                return False
        age = self.data_age_hours(self.latest_data_time())
        if age is None:
            return True          # 判定不了就允许，宁可多采也不卡住用户
        return age * 60.0 >= self.manual_gap_minutes

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._state["running"])

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        try:
            if self._needs_refresh():
                self._run_once("startup")
            while not self._stop.is_set():
                with self._lock:
                    self._state["next_run"] = iso(
                        utcnow() + timedelta(minutes=self.interval_minutes)
                    )
                # 到点自动跑，或被 trigger()/stop() 提前唤醒
                woke = self._wake.wait(timeout=self.interval_minutes * 60.0)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._run_once("manual" if woke else "auto")
        finally:
            with self._lock:
                self._state["enabled"] = False
                self._state["next_run"] = None

    def _needs_refresh(self) -> bool:
        """库内数据是否已过期（或无法判定）。"""
        if self.stale_hours <= 0:
            return True
        age = self.data_age_hours(self.latest_data_time())
        return age is None or age > self.stale_hours

    # ------------------------------------------------------------------
    # 单轮
    # ------------------------------------------------------------------
    def _run_once(self, reason: str = "auto") -> Dict[str, Any]:
        """执行一轮 采集 -> 聚类 -> 打分。异常被吞掉并记录，不向上抛。"""
        with self._lock:
            if self._state["running"]:
                return {"skipped": "上一轮尚未结束"}
            self._state["running"] = True
            self._state["last_reason"] = reason
            self._state["last_start"] = iso(utcnow())
        started = time.time()
        print(f"[AUTO] 第 {self._state['runs'] + 1} 轮开始（{reason}），"
              f"周期 {self.interval_minutes:g} 分钟", flush=True)

        summary: Optional[Dict[str, Any]] = None
        ok = False
        err: Optional[str] = None
        try:
            config.ensure_dirs()
            conn = store.connect()
            try:
                store.init_db(conn)
                sources = config.load_sources(only_enabled=True)
                runs = collect.collect_all(conn, sources, delay=self.delay,
                                           verbose=self.verbose)
                agg = collect.summarize(runs)
                cluster.cluster_articles(conn, days=self.days, verbose=self.verbose)
                scored = credibility.score_all(conn, tau=self.tau, verbose=self.verbose)
                # 事件关键词标签：纯自研算法、全库 0.3 秒，所以直接放进每轮流水线，
                # 界面上"标签"这一列才是跟着数据一起更新的
                tags = keywords.tag_all(conn, verbose=self.verbose)
                # 新闻分区：同样是纯自研、全库秒级。
                # 不放进流水线的话，新采文章的 topic 一直是空串，
                # 界面上按分区筛选时这些新稿子会直接“消失”。
                topic_res = topics.classify_all(conn, verbose=False, only_missing=True)
                summary = {"collect": dict(agg), "score": dict(scored),
                           "tags": dict(tags),
                           "topics": dict(topic_res.get("by_topic") or {}),
                           "sources": len(sources)}
            finally:
                conn.close()
            ok = True
        except Exception as exc:                      # 单轮失败不致命
            err = f"{type(exc).__name__}: {exc}"
        finally:
            elapsed = round(time.time() - started, 1)
            with self._lock:
                self._state["running"] = False
                self._state["runs"] += 1
                self._state["last_finish"] = iso(utcnow())
                self._state["last_elapsed"] = elapsed
                self._state["last_ok"] = ok
                self._state["last_summary"] = summary
                self._state["last_error"] = err
                if not ok:
                    self._state["fails"] += 1

        if ok:
            new = (summary or {}).get("collect", {}).get("new", "?")
            print(f"[AUTO] 完成：新增 {new} 条，用时 {elapsed}s", flush=True)
        else:
            print(f"[AUTO] 失败：{err}（用时 {elapsed}s，等待下一周期重试）", flush=True)
        return {"ok": ok, "elapsed": elapsed, "error": err, "summary": summary}
