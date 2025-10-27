from __future__ import annotations

import logging
import asyncio
import math
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional, Set, Any, Union, cast
from flask import Flask
from threading import Thread
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue,
)
from telegram.constants import ParseMode
import re
import json

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# API Base URLs
OPENF1_API = "https://api.openf1.org/v1"
JOLPICA_API = "https://api.jolpi.ca/ergast/f1/"
# Community-maintained F1 calendar (raw JSON on GitHub) - now with dynamic year
F1_CALENDAR_API_TEMPLATE = (
    "https://raw.githubusercontent.com/sportstimes/f1/main/_db/f1/{year}.json"
)

# Tyre compound emojis
TYRE_COMPOUNDS = {
    "SOFT": "🔴 Soft",  # Red square
    "MEDIUM": "🟡 Medium",  # Yellow circle
    "HARD": "⚪ Hard",  # White circle
    "INTERMEDIATE": "🔵 Intermediate",  # Blue circle
    "WET": "🟢 Wet",  # Green circle
    "UNKNOWN": "⚫ Unknown",  # Black circle (Fallback)
}

# Simple country to flag emoji map (limited set for F1 calendar)
FLAG_EMOJIS = {
    "Bahrain": "🇧🇭",
    "Saudi Arabia": "🇸🇦",
    "Australia": "🇦🇺",
    "Japan": "🇯🇵",
    "China": "🇨🇳",
    "United States": "🇺🇸",
    "Monaco": "🇲🇨",
    "Canada": "🇨🇦",
    "Spain": "🇪🇸",
    "Austria": "🇦🇹",
    "Great Britain": "🇬🇧",
    "Hungary": "🇭🇺",
    "Belgium": "🇧🇪",
    "Netherlands": "🇳🇱",
    "Italy": "🇮🇹",
    "Azerbaijan": "🇦🇿",
    "Singapore": "🇸🇬",
    "Mexico": "🇲🇽",
    "Brazil": "🇧🇷",
    "Qatar": "🇶🇦",
    "UAE": "🇦🇪",
    "Portugal": "🇵🇹",
    "Turkey": "🇹🇷",
    "France": "🇫🇷",
    "Germany": "🇩🇪",
    "South Africa": "🇿🇦",
    "Korea": "🇰🇷",
    "India": "🇮🇳",
}

# Baku, Azerbaijan Timezone Offset (UTC+4)
BAKU_TIME_OFFSET = timedelta(hours=4)


class F1LiveDashboard:
    def __init__(self):
        # Session management
        self._session: Optional[aiohttp.ClientSession] = None

        # Live tracking state
        self.current_session_key: Optional[int] = None
        self.live_messages: Dict[int, int] = {}
        self.update_tasks: Dict[int, asyncio.Task] = {}
        self.commentary_tasks: Dict[int, asyncio.Task] = {}
        self.commentary_seen: Dict[int, Set[str]] = {}
        # Per-chat auto-commentary preference
        self.auto_commentary: Set[int] = set()

        # User favorites and subscriptions
        self.user_favorites: Dict[int, int] = {}
        self.subscribed_chats: Set[int] = set()

        # Cache storage
        self.race_control_cache: Dict[int, List[Dict]] = {}
        self.standings_cache: Optional[List[Dict]] = None
        self.historical_data_cache: Dict[str, Dict] = {}
        self.dynamic_driver_abbr: Dict[int, str] = {}
        self.session_result_cache: Dict[int, List[Dict]] = {}  # New: for DNF/status

        # Cache timestamps
        self.last_rc_fetch: Optional[datetime] = None
        self.last_lineup_fetch: Optional[datetime] = None
        self.last_standings_fetch: Optional[datetime] = None
        self.last_schedule_fetch: Optional[datetime] = None
        self.last_session_fetch: Optional[datetime] = None
        self.last_session_result_fetch: Optional[datetime] = None  # New
        # Calendar cache
        self.calendar_cache: Optional[Dict] = None
        self.last_calendar_fetch: Optional[datetime] = None
        # Intervals cache for gaps
        self.intervals_cache: Dict[int, Dict[int, str]] = {}
        self.last_intervals_fetch: Optional[datetime] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session or not isinstance(self._session, aiohttp.ClientSession):
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_session_result(self, session_key: int) -> List[Dict]:
        """Fetches session results including status (DNF, etc.) from OpenF1."""
        if (
            not self.session_result_cache.get(session_key)
            or not self.last_session_result_fetch
            or (datetime.now() - self.last_session_result_fetch).total_seconds() > 30
        ):
            try:
                url = f"{OPENF1_API}/session_result?session_key={session_key}"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        results = await resp.json()
                        self.session_result_cache[session_key] = results
                        self.last_session_result_fetch = datetime.now()
            except Exception as e:
                logger.error(f"Error fetching session results: {e}")
                return []
        return self.session_result_cache.get(session_key, [])

    async def get_race_control(self, session_key: int) -> List[Dict]:
        """Fetches race control messages from OpenF1."""
        if (
            not self.race_control_cache.get(session_key)
            or not self.last_rc_fetch
            or (datetime.now() - self.last_rc_fetch).total_seconds() > 30
        ):
            try:
                url = f"{OPENF1_API}/race_control?session_key={session_key}&order_by=-date"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        messages = await resp.json()
                        self.race_control_cache[session_key] = messages
                        self.last_rc_fetch = datetime.now()
            except Exception as e:
                logger.error(f"Error fetching race control messages: {e}")
                return []
        return self.race_control_cache.get(session_key, [])

    async def get_session_events(self, session_key: int) -> List[Dict]:
        """Try several possible endpoints to fetch session event data (overtakes, pitstops, retirements, DNF)."""
        endpoints = [
            "overtakes",
            "overtake",
            "events",
            "incidents",
            "race_events",
            "pit_stops",
            "pitstops",
            "pit_stop",
            "retirements",
            "dnf",
            "pit",  # For pit stops
            "session_result",  # New: for DNF/status
        ]

        collected: List[Dict] = []
        for ep in endpoints:
            try:
                url = f"{OPENF1_API}/{ep}?session_key={session_key}"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                        except Exception:
                            text = await resp.text()
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = None

                        if not data:
                            continue

                        # If endpoint returns a list directly
                        if isinstance(data, list):
                            collected.extend(data)
                        # If returns dict with top-level list
                        elif isinstance(data, dict):
                            # Try common keys
                            for k in (
                                "events",
                                "overtakes",
                                "items",
                                "data",
                                "results",
                            ):
                                if isinstance(data.get(k), list):
                                    collected.extend(data.get(k))
                                    break
                            else:
                                # If dict looks like a single event, append
                                if data:
                                    collected.append(data)
            except Exception as e:
                logger.debug(f"Endpoint {ep} not available or failed: {e}")

        return collected

    # ... (other methods remain the same, like init_session, close_session, is_session_live, get_live_driver_lineup, etc.)

    async def get_live_timing(self, session_key: int) -> List[Dict]:
        """Fetches the latest position and status for all drivers in the session."""
        try:
            url = f"{OPENF1_API}/position?session_key={session_key}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    positions = await resp.json()
                    driver_positions = {}
                    for pos in positions:
                        driver_num = pos.get("driver_number")
                        if driver_num and driver_num not in driver_positions:
                            driver_positions[driver_num] = pos

                    sorted_positions = sorted(
                        driver_positions.values(), key=lambda x: x.get("position", 999)
                    )
                    return sorted_positions
        except Exception as e:
            logger.error(f"Error fetching live timing: {e}")
        return []

    async def get_lap_times(self, session_key: int) -> Dict[int, str]:
        """Get the latest completed lap time for each driver."""
        try:
            url = f"{OPENF1_API}/laps?session_key={session_key}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    laps = await resp.json()
                    driver_laps = {}
                    for lap in laps:
                        driver_num = lap.get("driver_number")
                        lap_duration = lap.get("lap_duration")
                        if (
                            driver_num
                            and lap_duration
                            and driver_num not in driver_laps
                        ):
                            minutes = int(lap_duration // 60)
                            seconds = lap_duration % 60
                            lap_time = f"{minutes}:{seconds:06.3f}"
                            driver_laps[driver_num] = lap_time
                    return driver_laps
        except Exception as e:
            logger.error(f"Error fetching lap times: {e}")
        return {}

    async def get_stints(self, session_key: int) -> Dict[int, str]:
        """Get the current tyre compound for each driver (latest stint)."""
        try:
            url = f"{OPENF1_API}/stints?session_key={session_key}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    stints = await resp.json()
                    driver_stints = {}
                    for stint in stints:
                        driver_num = stint.get("driver_number")
                        compound = stint.get("compound", "UNKNOWN")
                        if driver_num and driver_num not in driver_stints:
                            driver_stints[driver_num] = compound.upper()
                    return driver_stints
        except Exception as e:
            logger.error(f"Error fetching stints: {e}")
        return {}

    async def get_intervals(self, session_key: int) -> Dict[int, str]:
        """Get the latest intervals (gaps) for each driver."""
        if (
            not self.intervals_cache.get(session_key)
            or not self.last_intervals_fetch
            or (datetime.now() - self.last_intervals_fetch).total_seconds() > 15
        ):
            try:
                url = f"{OPENF1_API}/intervals?session_key={session_key}"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        intervals = await resp.json()
                        driver_intervals = {}
                        for inter in intervals:
                            driver_num = inter.get("driver_number")
                            gap = inter.get("gap_to_leader")
                            if (
                                driver_num
                                and gap
                                and driver_num not in driver_intervals
                            ):
                                driver_intervals[driver_num] = gap
                        self.intervals_cache[session_key] = driver_intervals
                        self.last_intervals_fetch = datetime.now()
            except Exception as e:
                logger.error(f"Error fetching intervals: {e}")
                return {}
        return self.intervals_cache.get(session_key, {})

    async def get_fastest_lap(self, session_key: int) -> Optional[Dict]:
        """Determines the current fastest lap based on lap times."""
        try:
            lap_times = await self.get_lap_times(session_key)
            if not lap_times:
                return None

            def time_to_seconds(time_str):
                try:
                    m, s = map(float, time_str.split(":"))
                    return m * 60 + s
                except:
                    return 99999.0

            valid_times = [
                time_to_seconds(t)
                for t in lap_times.values()
                if time_to_seconds(t) < 99999.0
            ]
            if not valid_times:
                return None

            fastest_time_seconds = min(valid_times)

            fastest_lap_driver_num = next(
                (
                    d_num
                    for d_num, t_str in lap_times.items()
                    if math.isclose(
                        time_to_seconds(t_str), fastest_time_seconds, rel_tol=1e-9
                    )
                ),
                None,
            )

            if fastest_lap_driver_num:
                return {
                    "driver_number": fastest_lap_driver_num,
                    "lap_time": lap_times[fastest_lap_driver_num],
                }

        except Exception as e:
            logger.error(f"Error fetching fastest lap: {e}")
        return None

    def format_dashboard(
        self,
        session_info: Dict[str, Any],
        positions: List[Dict[str, Any]],
        lap_times: Dict[int, str],
        tyres: Dict[int, str],
        favorite: Optional[int] = None,
        fastest_lap_data: Optional[Dict[str, Any]] = None,
        intervals: Dict[int, str] = None,
        session_results: List[Dict] = None,  # New: for DNF status
    ) -> str:
        """Format the live dashboard message with real gaps, all drivers, and DNF status."""
        # Get values with safe fallbacks
        race_name = session_info.get("meeting_name", "F1 Race")
        circuit_name = session_info.get("circuit_short_name", "Track")
        session_name = session_info.get("session_name", "Session")

        lines = [
            f"📍 <b>{race_name} ({circuit_name})</b>",
            "🏎️ <b>F1 Live Dashboard</b>",
            f"🏁 {session_name}",
        ]

        driver_abbr = self.dynamic_driver_abbr

        # Create driver status map from session_results
        driver_status = {}
        if session_results:
            for result in session_results:
                driver_num = result.get("driver_number")
                status = result.get("status", "")
                if (
                    driver_num
                    and status
                    and "dnf" in status.lower()
                    or "retired" in status.lower()
                ):
                    driver_status[driver_num] = "DNF"

        fastest_lap_driver_num = None
        if fastest_lap_data:
            driver_number = fastest_lap_data.get("driver_number")
            if driver_number is not None:
                fastest_lap_driver_num = driver_number
                fl_abbr = f"DR{driver_number}"
                if driver_number in driver_abbr:
                    fl_abbr = driver_abbr[driver_number]
                fl_time = fastest_lap_data.get("lap_time", "---")
                lines.append(f"🟣 <b>Fastest Lap:</b> {fl_abbr} ({fl_time})")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        # Determine current lap from positions if available
        current_lap = None
        try:
            laps_found = (
                [
                    int(
                        p.get("lap")
                        or p.get("laps")
                        or p.get("lap_number")
                        or p.get("current_lap")
                        or 0
                    )
                    for p in positions
                    if isinstance(p, dict)
                ]
                if positions
                else []
            )
            if laps_found:
                current_lap = max(laps_found)
        except Exception:
            current_lap = None

        total_laps = None
        for k in ("lap_count", "laps", "race_laps", "total_laps"):
            try:
                v = session_info.get(k)
                if v:
                    total_laps = int(v)
                    break
            except Exception:
                continue

        if current_lap is not None:
            if total_laps:
                lines.append(f"🏁 <i>Lap {current_lap} / {total_laps}</i>")
            else:
                lines.append(f"🏁 <i>Lap {current_lap}</i>")

        if not positions:
            lines.append(
                "\n⏳ <i>Waiting for live data (OpenF1 data is often sparse)...</i>"
            )
            return "\n".join(lines)

        for pos_data in positions:
            driver_num = pos_data.get("driver_number")
            if driver_num is None:
                continue

            position = pos_data.get("position")
            if position is None:
                position = "?"

            driver_abbr_code = f"DR{driver_num}"
            if driver_num in driver_abbr:
                driver_abbr_code = driver_abbr[driver_num]

            # Add DNF status if applicable
            dnf_status = " (DNF)" if driver_num in driver_status else ""
            driver_abbr_code += dnf_status

            try:
                pos_num = int(position) if isinstance(position, (int, str)) else 0
                is_live_flag = self.is_session_live(session_info)
                if not is_live_flag:
                    pos_emoji = (
                        "🥇"
                        if pos_num == 1
                        else (
                            "🥈" if pos_num == 2 else ("🥉" if pos_num == 3 else "  ")
                        )
                    )
                else:
                    pos_emoji = "  "
            except (ValueError, TypeError):
                pos_emoji = "  "

            gap = "+?.???"
            if intervals and driver_num in intervals:
                gap = intervals[driver_num]
                if isinstance(gap, (int, float)):
                    gap = f"+{float(gap):.3f}"
                elif "LAP" in str(gap).upper():
                    gap = str(gap)
            elif pos_num > 1:
                gap = f"+{pos_num * 0.5:.3f}"

            lap_time = "-:--.---"
            if driver_num in lap_times:
                lap_time = lap_times[driver_num]
                if isinstance(lap_time, str) and len(lap_time) > 10:
                    lap_time = lap_time[:10]

            try:
                driver_lap = (
                    pos_data.get("lap")
                    or pos_data.get("laps")
                    or pos_data.get("lap_number")
                    or pos_data.get("current_lap")
                )
                if driver_lap is None:
                    driver_lap_str = ""
                else:
                    driver_lap_str = f" | L:{int(driver_lap)}"
            except Exception:
                driver_lap_str = ""

            compound = "UNKNOWN"
            if driver_num in tyres:
                compound = tyres[driver_num]
            tyre_text = TYRE_COMPOUNDS.get(compound, "⚫ Unknown")

            fl_indicator = "🟣" if driver_num == fastest_lap_driver_num else "  "

            prefix = "➤ " if driver_num == favorite else ""

            line = f"{prefix}{pos_emoji} <b>P{position:02}</b> {fl_indicator} {driver_abbr_code} | {gap} | {lap_time}{driver_lap_str} | {tyre_text}"
            lines.append(line)

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        baku_tz = timezone(BAKU_TIME_OFFSET)
        lines.append(
            f"🔄 <i>Updated: {datetime.now(baku_tz).strftime('%H:%M:%S Baku Time')}</i>"
        )

        return "\n".join(lines)

    def format_race_control(self, messages: List[Dict]) -> str:
        """Format the Race Control messages."""
        if not messages:
            return "⚠️ <b>Race Control</b>\n━━━━━━━━━━━━━━━━\n<i>No recent messages</i>"

        lines = ["⚠️ <b>Race Control Messages</b>", "━━━━━━━━━━━━━━━━━━━━"]

        for msg in messages[:5]:
            category = msg.get("category", "Info")
            message = msg.get("message", "")
            flag = msg.get("flag", "")

            flag_emoji = ""
            if "YELLOW" in flag.upper():
                flag_emoji = "🟨"
            elif "RED" in flag.upper():
                flag_emoji = "🟥"
            elif "GREEN" in flag.upper():
                flag_emoji = "🟩"
            elif "CHEQUERED" in flag.upper() or "CHECKERED" in flag.upper():
                flag_emoji = "🏁"

            time_str = (
                msg.get("date", "")[:19].replace("T", " ") if msg.get("date") else ""
            )

            lines.append(f"{flag_emoji} <b>{category}</b>: {message}")
            if time_str:
                lines.append(f"   <i>{time_str} UTC</i>")

        return "\n".join(lines)

    # ... (other methods like get_next_race_meeting, get_last_race_results_summary, format_session_summary, get_latest_session, etc. remain the same)


async def commentary_loop(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, session_key: int
):
    """Background task: periodically fetch race control, session events, and DNF from session_result and send new items as messages."""
    try:
        await dashboard.get_live_driver_lineup()

        dashboard.commentary_seen.setdefault(chat_id, set())
        seen_dnfs = set()  # Track seen DNF drivers

        while True:
            try:
                rc_msgs = await dashboard.get_race_control(session_key)
                events = await dashboard.get_session_events(session_key)
                session_results = await dashboard.get_session_result(
                    session_key
                )  # New: fetch results for DNF

                # Process race control messages
                for msg in reversed(rc_msgs):
                    key = json.dumps(msg, sort_keys=True)
                    if key in dashboard.commentary_seen[chat_id]:
                        continue
                    dashboard.commentary_seen[chat_id].add(key)

                    title = msg.get("title") or msg.get("type") or "Race Control"
                    body = (
                        msg.get("message")
                        or msg.get("description")
                        or msg.get("text")
                        or str(msg)
                    )
                    text = f"⚠️ <b>{title}</b>\n{body}"
                    await context.bot.send_message(
                        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
                    )

                # Process other events (overtakes, pit stops, etc.)
                for ev in events:
                    key = json.dumps(ev, sort_keys=True)
                    if key in dashboard.commentary_seen[chat_id]:
                        continue
                    dashboard.commentary_seen[chat_id].add(key)

                    ev_type = (ev.get("type") or ev.get("event") or "").strip()
                    ev_text = (
                        ev.get("description")
                        or ev.get("short_text")
                        or ev.get("message")
                        or str(ev)
                    ).strip()

                    def code_for_num(n: int) -> str:
                        try:
                            num = int(n)
                            return dashboard.dynamic_driver_abbr.get(num, f"DR{num}")
                        except (ValueError, TypeError):
                            return str(n)
                        except Exception:
                            return f"DR{n}"

                    def find_lap(text: str, ev: Dict) -> Optional[int]:
                        for k in ("lap", "lap_number", "laps", "current_lap"):
                            v = ev.get(k)
                            if isinstance(v, (int, str)):
                                try:
                                    return int(v)
                                except Exception:
                                    continue
                        m = re.search(
                            r"lap(?:s)?\s*(?:#?:)?\s*(\d{1,3})",
                            text,
                            flags=re.IGNORECASE,
                        )
                        if m:
                            try:
                                return int(m.group(1))
                            except Exception:
                                return None
                        m2 = re.search(r"on lap\s*(\d{1,3})", text, flags=re.IGNORECASE)
                        if m2:
                            try:
                                return int(m2.group(1))
                            except Exception:
                                return None
                        return None

                    formatted = None

                    # Detect overtakes, pits, etc. (same as before)
                    if "overtake" in ev_type.lower() or "overtook" in ev_text.lower():
                        # ... (same logic as before)
                        pass  # Omitted for brevity

                    if not formatted and (
                        "pit" in ev_type.lower() or "pit" in ev_text.lower()
                    ):
                        # ... (same pit logic)
                        pass  # Omitted

                    # New: Detect DNF from events or session_result
                    if not formatted and (
                        "dnf" in ev_type.lower()
                        or "retir" in ev_type.lower()
                        or "retire" in ev_text.lower()
                    ):
                        dn = ev.get("driver_number") or ev.get("driver")
                        code = None
                        try:
                            if isinstance(dn, (int, str)):
                                code = code_for_num(dn)
                        except Exception:
                            code = None
                        if not code:
                            m = re.search(r"DR?(\d{1,2})", ev_text)
                            if m:
                                code = code_for_num(int(m.group(1)))
                        reason = ev_text
                        if code:
                            formatted = f"❌ <b>Retirement</b>\n{code} — {reason}"
                        else:
                            formatted = f"❌ <b>Retirement</b>\n{reason}"

                    if not formatted:
                        try:
                            ev_text = re.sub(
                                r"DR(\d{1,2})",
                                lambda m: code_for_num(int(m.group(1))),
                                ev_text,
                            )
                        except Exception:
                            pass
                        formatted = f"ℹ️ <b>{ev_type or 'Event'}</b>\n{ev_text}"

                    await context.bot.send_message(
                        chat_id=chat_id, text=formatted, parse_mode=ParseMode.HTML
                    )

                # New: Process DNF from session_result
                for result in session_results:
                    driver_num = result.get("driver_number")
                    status = result.get("status", "").lower()
                    if (
                        driver_num
                        and ("dnf" in status or "retired" in status)
                        and driver_num not in seen_dnfs
                    ):
                        code = code_for_num(driver_num)
                        dnf_key = f"dnf_{driver_num}"
                        if dnf_key not in dashboard.commentary_seen[chat_id]:
                            dashboard.commentary_seen[chat_id].add(dnf_key)
                            seen_dnfs.add(driver_num)
                            reason = result.get("status_detail", "Retirement")
                            dnf_text = f"❌ <b>DNF</b>\n{code} retired — {reason}"
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=dnf_text,
                                parse_mode=ParseMode.HTML,
                            )

                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in commentary loop for chat {chat_id}: {e}")
                await asyncio.sleep(15)
    except asyncio.CancelledError:
        logger.info(f"Commentary task cancelled for chat {chat_id}")


async def update_dashboard_loop(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, session_key: int
):
    """Background task to update the dashboard with DNF status."""
    try:
        await asyncio.sleep(3)

        while True:
            try:
                session_info = await dashboard.get_latest_session()
                if not dashboard.is_session_live(session_info):
                    raise asyncio.CancelledError(
                        "Session finished, stopping live updates."
                    )

                positions = await dashboard.get_live_timing(session_key)
                lap_times = await dashboard.get_lap_times(session_key)
                tyres = await dashboard.get_stints(session_key)
                intervals = await dashboard.get_intervals(session_key)
                fastest_lap_data = await dashboard.get_fastest_lap(session_key)
                session_results = await dashboard.get_session_result(session_key)  # New
                favorite = dashboard.user_favorites.get(chat_id)

                if session_info:
                    text = dashboard.format_dashboard(
                        session_info,
                        positions,
                        lap_times,
                        tyres,
                        favorite,
                        fastest_lap_data,
                        intervals,
                        session_results,
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "⏹️ Stop Updates", callback_data="stop"
                            ),
                            InlineKeyboardButton(
                                "💬 Commentary", callback_data="commentary"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "🗓️ Weekend Schedule", callback_data="schedule"
                            )
                        ],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                    )

                await asyncio.sleep(15)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in update loop: {e}")
                await asyncio.sleep(30)

    except asyncio.CancelledError:
        logger.info(f"Update task cancelled for chat {chat_id}")
        static_message = "⏹️ Live updates for the session have ended."
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=static_message,
            parse_mode=ParseMode.HTML,
        )


# ... (rest of the code remains the same: live_command, stop_command, simulate_live_loop with DNF from FastF1 if needed, etc.)


# For simulation, add DNF detection from FastF1
async def simulate_live_loop(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
):
    """Simulated live loop using FastF1 historical data for demo/testing, with DNF."""
    await dashboard.get_live_driver_lineup()

    try:
        year = 2025
        gp = "Mexico"
        session_type = "R"
        session = await asyncio.get_event_loop().run_in_executor(
            None, lambda: fastf1.get_session(year, gp, session_type)
        )
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: session.load(telemetry=True, laps=True, weather=True)
        )

        laps = session.laps
        max_laps = int(laps["LapNumber"].max())

        def format_lap_time(lap_time):
            if pd.isna(lap_time):
                return "1:31.000"
            seconds = lap_time.total_seconds()
            minutes = int(seconds // 60)
            seconds = seconds % 60
            return f"{minutes}:{seconds:06.3f}"

        # Get full results for DNF
        results = session.results
        dnf_drivers = set(results[results["Status"] == "DNF"]["DriverNumber"].tolist())

        previous_positions = {}

        for lap in range(1, max_laps + 1):
            current_laps = laps[laps["LapNumber"] == lap]
            if current_laps.empty:
                continue

            current_laps = current_laps.sort_values("Position")
            if current_laps["Position"].isnull().all():
                current_laps = current_laps.sort_values("LapTime")

            positions = []
            current_pos_dict = {}
            for idx, row in current_laps.iterrows():
                driver_num = (
                    int(row["DriverNumber"])
                    if pd.notnull(row["DriverNumber"])
                    else idx + 1
                )
                position = (
                    int(row["Position"]) if pd.notnull(row["Position"]) else idx + 1
                )
                positions.append({"driver_number": driver_num, "position": position})
                current_pos_dict[driver_num] = position

            # Detect overtakes (same as before)
            for driver_num, pos in current_pos_dict.items():
                if driver_num in previous_positions:
                    prev_pos = previous_positions[driver_num]
                    if pos < prev_pos:
                        for other_num, other_pos in current_pos_dict.items():
                            if other_pos == prev_pos and other_num != driver_num:
                                a_code = dashboard.dynamic_driver_abbr.get(
                                    driver_num, f"DR{driver_num}"
                                )
                                b_code = dashboard.dynamic_driver_abbr.get(
                                    other_num, f"DR{other_num}"
                                )
                                overtake_text = f"🔁 <b>Overtake (L{lap})</b>\n{a_code} overtook {b_code}"
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=overtake_text,
                                    parse_mode=ParseMode.HTML,
                                )
                                break

            previous_positions = current_pos_dict

            lap_times = {}
            for idx, row in current_laps.iterrows():
                driver_num = (
                    int(row["DriverNumber"])
                    if pd.notnull(row["DriverNumber"])
                    else idx + 1
                )
                lap_time = row["LapTime"]
                lap_times[driver_num] = format_lap_time(lap_time)

            tyres = {}
            for idx, row in current_laps.iterrows():
                driver_num = (
                    int(row["DriverNumber"])
                    if pd.notnull(row["DriverNumber"])
                    else idx + 1
                )
                compound = row.get("Compound", "UNKNOWN").upper()
                tyres[driver_num] = compound

            leader_lap = (
                current_laps.iloc[0]["LapTime"]
                if not current_laps.empty
                else pd.Timedelta(0)
            )

            intervals = {}
            for idx, row in current_laps.iterrows():
                driver_num = (
                    int(row["DriverNumber"])
                    if pd.notnull(row["DriverNumber"])
                    else idx + 1
                )
                lap_time = row["LapTime"]
                gap_sec = (
                    (lap_time - leader_lap).total_seconds()
                    if pd.notnull(lap_time)
                    else 0.0
                )
                intervals[driver_num] = f"{gap_sec:.3f}"

            # Session results for DNF in simulation
            session_results_sim = []
            for _, result in results.iterrows():
                driver_num = result["DriverNumber"]
                status = result["Status"]
                if status == "DNF":
                    session_results_sim.append(
                        {"driver_number": driver_num, "status": status}
                    )

            session_info = {
                "meeting_name": "Demo Replay - Mexico 2025",
                "circuit_short_name": "Mexico City",
                "session_name": "Race",
            }

            text = dashboard.format_dashboard(
                session_info,
                positions,
                lap_times,
                tyres,
                None,
                None,
                intervals,
                session_results_sim,
            )

            kb = [
                [
                    InlineKeyboardButton("⏹️ Stop Updates", callback_data="stop"),
                    InlineKeyboardButton("💬 Commentary", callback_data="commentary"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(kb)

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )

            # Send DNF message if driver DNFs this lap (simple: if not in previous laps)
            for driver_num in dnf_drivers:
                if driver_num in current_pos_dict:
                    continue  # Still running
                # Assume DNF message if first time missing
                dnf_text = f"❌ <b>DNF</b>\n{dashboard.dynamic_driver_abbr.get(driver_num, f'DR{driver_num}')} retired"
                await context.bot.send_message(
                    chat_id=chat_id, text=dnf_text, parse_mode=ParseMode.HTML
                )

            await asyncio.sleep(2)

    except asyncio.CancelledError:
        logger.info(f"Simulation loop cancelled for chat {chat_id}")


# ... (rest of the bot code remains the same: start, help_command, live_command, etc.)
