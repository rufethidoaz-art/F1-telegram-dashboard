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
    JobQueue
)
from telegram.constants import ParseMode
import re

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
logger = logging.getLogger(__name__)

import json

# Additional imports for FastF1 simulation in demo
import fastf1
import pandas as pd

# API Base URLs
OPENF1_API = "https://api.openf1.org/v1"
JOLPICA_API = "https://api.jolpi.ca/ergast/f1/" 
# Community-maintained F1 calendar (raw JSON on GitHub) - now with dynamic year
F1_CALENDAR_API_TEMPLATE = "https://raw.githubusercontent.com/sportstimes/f1/main/_db/f1/{year}.json"

# Tyre compound emojis
TYRE_COMPOUNDS = {
    "SOFT": "🔴 Soft",      # Red square
    "MEDIUM": "🟡 Medium",     # Yellow circle
    "HARD": "⚪ Hard",       # White circle
    "INTERMEDIATE": "🔵 Intermediate", # Blue circle
    "WET": "🟢 Wet",       # Green circle
    "UNKNOWN": "⚫ Unknown",     # Black circle (Fallback)
}

# Simple country to flag emoji map (limited set for F1 calendar)
FLAG_EMOJIS = {
    "Bahrain": "🇧🇭", "Saudi Arabia": "🇸🇦", "Australia": "🇦🇺", "Japan": "🇯🇵",
    "China": "🇨🇳", "United States": "🇺🇸", "Monaco": "🇲🇨", "Canada": "🇨🇦",
    "Spain": "🇪🇸", "Austria": "🇦🇹", "Great Britain": "🇬🇧", "Hungary": "🇭🇺",
    "Belgium": "🇧🇪", "Netherlands": "🇳🇱", "Italy": "🇮🇹", "Azerbaijan": "🇦🇿",
    "Singapore": "🇸🇬", "Mexico": "🇲🇽", "Brazil": "🇧🇷", "Qatar": "🇶🇦",
    "UAE": "🇦🇪", "Portugal": "🇵🇹", "Turkey": "🇹🇷", "France": "🇫🇷", 
    "Germany": "🇩🇪", "South Africa": "🇿🇦", "Korea": "🇰🇷", "India": "🇮🇳"
}

# Baku, Azerbaijan Timezone Offset (UTC+4)
# This is used for displaying the schedule in the user's requested timezone.
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
        # Per-chat auto-commentary preference (if present, commentary starts automatically when live updates start)
        self.auto_commentary: Set[int] = set()

        # User favorites and subscriptions
        self.user_favorites: Dict[int, int] = {}
        self.subscribed_chats: Set[int] = set()
        
        # Cache storage
        self.race_control_cache: Dict[int, List[Dict]] = {}
        self.standings_cache: Optional[List[Dict]] = None
        self.historical_data_cache: Dict[str, Dict] = {}
        self.dynamic_driver_abbr: Dict[int, str] = {}
        
        # Cache timestamps
        self.last_rc_fetch: Optional[datetime] = None
        self.last_lineup_fetch: Optional[datetime] = None
        self.last_standings_fetch: Optional[datetime] = None
        self.last_schedule_fetch: Optional[datetime] = None
        self.last_session_fetch: Optional[datetime] = None
        # Calendar cache (community JSON)
        self.calendar_cache: Optional[Dict] = None
        self.last_calendar_fetch: Optional[datetime] = None
        # Add intervals cache for gaps
        self.intervals_cache: Dict[int, Dict[int, str]] = {}
        self.last_intervals_fetch: Optional[datetime] = None

        # For FastF1 cache in demo
        fastf1.Cache.enable_cache('fastf1_cache')  # Create a cache directory for FastF1

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session or not isinstance(self._session, aiohttp.ClientSession):
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_race_control(self, session_key: int) -> List[Dict]:
        """Fetches race control messages from OpenF1."""
        if (not self.race_control_cache.get(session_key) or 
            not self.last_rc_fetch or 
            (datetime.now() - self.last_rc_fetch).total_seconds() > 30):
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
        """Try several possible endpoints to fetch session event data (overtakes, pitstops, retirements).

        This function is defensive: it attempts multiple likely endpoint names and aggregates
        any responses (200). It returns a flattened list of event dicts.
        """
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
                            for k in ("events", "overtakes", "items", "data", "results"):
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

    async def init_session(self):
        """Initialize aiohttp session"""
        if not self._session or not isinstance(self._session, aiohttp.ClientSession):
            self._session = aiohttp.ClientSession()
        return self.session

    async def close_session(self):
        """Close aiohttp session"""
        if isinstance(self._session, aiohttp.ClientSession):
            await self._session.close()
            self._session = None

    def is_session_live(self, session_info: Optional[Dict]) -> bool:
        """
        Determine if a session is currently live based on current time and session data.
        Returns False if the session is finished or cancelled.
        """
        if not session_info:
            return False
            
        session_status = session_info.get('session_status')
        if session_status and session_status.lower() in ['finished', 'cancelled', 'completed']:
            return False
            
        date_start_str = session_info.get('date_start')
        date_end_str = session_info.get('date_end')
        
        if not date_start_str or not date_end_str:
            return True # Assume live if times are missing, forcing API check

        try:
            now_utc = datetime.now(timezone.utc)
            start = datetime.fromisoformat(date_start_str.replace('Z', '+00:00'))
            end = datetime.fromisoformat(date_end_str.replace('Z', '+00:00'))
            pre_session_buffer = timedelta(minutes=15)

            # Session is live if now is between start and end time
            return (start - pre_session_buffer) <= now_utc <= end
        except Exception as e:
            logger.warning(f"Error parsing session times for live check: {e}")
            return True # Fallback if parsing fails

    # --- Jolpica-F1 (Driver Lineup & Standings) Implementation ---

    async def get_live_driver_lineup(self) -> Dict[int, str]:
        """Fetches the current driver lineup (number -> code mapping) and caches it."""
        # Caching logic remains the same
        if self.dynamic_driver_abbr and self.last_lineup_fetch and \
           (datetime.now() - self.last_lineup_fetch).total_seconds() < 86400:
            return self.dynamic_driver_abbr
        
        try:
            url = f"{JOLPICA_API}current/driverStandings.json"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    standings_data = data.get('MRData', {}).get('StandingsTable', {}).get(
                        'StandingsLists', [{}])[0].get('DriverStandings', [])
                    
                    if standings_data:
                        new_mapping = {}
                        for entry in standings_data:
                            driver = entry.get('Driver', {})
                            driver_number_str = driver.get('permanentNumber')
                            driver_code = driver.get('code')
                            
                            if driver_number_str and driver_code:
                                try:
                                    driver_number = int(driver_number_str)
                                    new_mapping[driver_number] = driver_code.upper()
                                except ValueError:
                                    continue
                                    
                        self.dynamic_driver_abbr = new_mapping
                        self.last_lineup_fetch = datetime.now()
                        return new_mapping
        except Exception as e:
            logger.error(f"Error fetching driver lineup: {e}")
            
        return self.dynamic_driver_abbr if self.dynamic_driver_abbr else {}


    async def get_championship_standings(self) -> Optional[List[Dict]]:
        """Fetches current Driver Standings from the Jolpica-F1 API (Ergast-compatible)."""
        # Caching logic remains the same
        if self.standings_cache and self.last_standings_fetch and \
           (datetime.now() - self.last_standings_fetch).total_seconds() < 3600:
            return self.standings_cache
        
        try:
            url = f"{JOLPICA_API}current/driverStandings.json"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    standings_data = data.get('MRData', {}).get('StandingsTable', {}).get(
                        'StandingsLists', [{}])[0].get('DriverStandings', [])
                    
                    if standings_data:
                        self.standings_cache = standings_data
                        self.last_standings_fetch = datetime.now()
                        return standings_data
        except Exception as e:
            logger.error(f"Error fetching standings from Jolpica: {e}")
        
        return None

    def format_standings(self, standings: List[Dict]) -> str:
        """Format the Jolpica standings data into a readable message."""
        # Formatting remains the same
        if not standings:
            return "❌ <b>Championship Standings</b>\n━━━━━━━━━━━━━━━━━━━━\n<i>Data currently unavailable.</i>"

        lines = [
            "🏆 <b>Driver Standings (Current Season)</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            "<b>POS | DRV | TEAM             | PTS</b>"
        ]
        
        driver_abbr = self.dynamic_driver_abbr
        
        for driver_standing in standings:
            position = driver_standing.get('position', '?')
            points = driver_standing.get('points', '0')
            driver_data = driver_standing.get('Driver', {})
            constructor_data = driver_standing.get('Constructors', [{}])[0]
            
            driver_number_str = driver_data.get('permanentNumber')
            driver_code = driver_data.get('code', '???').upper()

            try:
                driver_num_int = int(driver_number_str)
                driver_code = driver_abbr.get(driver_num_int, driver_code)
            except (ValueError, TypeError):
                pass
            
            team_name = constructor_data.get('name', 'N/A')
            
            team_abbr = team_name.replace('Red Bull Racing', 'Red Bull')\
                                 .replace('Aston Martin Aramco Mercedes', 'Aston Martin')\
                                 .replace('Oracle Red Bull Racing', 'Red Bull')\
                                 .split()[0][:14].ljust(14) 
            
            line = f"<b>{position:3}</b> | {driver_code:3} | {team_abbr} | <b>{points}</b>"
            lines.append(line)

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"<i>Data Source: Jolpica-F1</i>")
        
        return "\n".join(lines)
    
    # --- Next Race and Last Session Summary Methods ---

    async def get_next_race_meeting(self) -> Optional[Dict]:
        """Fetches the next upcoming race meeting using Jolpica-F1."""
        try:
            url = f"{JOLPICA_API}current.json"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    races = data.get('MRData', {}).get('RaceTable', {}).get('Races', [])
                    now_utc = datetime.now(timezone.utc)
                    
                    for race in races:
                        race_date_str = race.get('date')
                        race_time_str = race.get('time', '00:00:00Z')
                        race_datetime_str = f"{race_date_str}T{race_time_str.replace('Z', '+00:00')}"
                        
                        try:
                            race_start = datetime.fromisoformat(race_datetime_str)
                            if race_start > now_utc:
                                # This is the next one. Map Ergast data to expected keys.
                                return {
                                    'meeting_name': race.get('raceName'),
                                    'location': race.get('Circuit', {}).get('Location', {}).get('locality'),
                                    'country_name': race.get('Circuit', {}).get('Location', {}).get('country'),
                                    'date_start': race_datetime_str,
                                    'date_end': race_datetime_str, # Use same for simplicity in display
                                    'circuit_key': race.get('Circuit', {}).get('circuitId'),
                                    'round': race.get('round'),
                                    'season': race.get('season')
                                }
                        except Exception:
                            continue
                    return None
        except Exception as e:
            logger.error(f"Error fetching next race meeting: {e}")
        return None

    # --- UPDATED: Get the summary of the most recently finished session (P, Q, or R) ---
    async def get_last_race_results_summary(self) -> Optional[Dict]:
        """
        Fetches the summary of the most recently finished F1 session (Race, Quali, or Practice)
        using OpenF1 data (best lap times). This uses a robust check based on session end time.
        """
        try:
            # 1. Find the most recently completed session (end time in the past)
            # Fetch the last 10 sessions to ensure we catch the one that just finished
            url = f"{JOLPICA_API}current/last/results.json"
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                races = data.get('MRData', {}).get('RaceTable', {}).get('Races', [])
                if not races:
                    return None
                
            last_race = races[0]
            meeting_name = last_race.get('raceName')
            session_name = "Race"
            results = last_race.get('Results', [])
            
            top_3 = []
            for result in results[:3]:
                driver_code = result.get('Driver', {}).get('code', 'N/A')
                position = result.get('position')
                top_3.append({
                    'position': position,
                    'driver': driver_code,
                    'best_time': result.get('Time', {}).get('time', 'Finished')
                })

            # Find fastest lap from results
            fastest_lap_result = next((res for res in results if res.get('FastestLap', {}).get('rank') == '1'), None)
            fastest_lap_data = None
            if fastest_lap_result:
                fastest_lap_data = {
                    'driver': fastest_lap_result.get('Driver', {}).get('code', 'N/A'),
                    'time': fastest_lap_result.get('FastestLap', {}).get('Time', {}).get('time', 'N/A')
                }
            
            summary = {
                'meeting_name': meeting_name,
                'session_name': session_name,
                'top_3': top_3,
                'fastest_lap': {
                    'driver': fastest_lap_data['driver'] if fastest_lap_data else 'N/A',
                    'time': fastest_lap_data['time'] if fastest_lap_data else 'N/A'
                },
                'is_race': 'RACE' in session_name.upper() or 'GRAND PRIX' in session_name.upper()
            }
            return summary

        except Exception as e:
            logger.error(f"Error fetching last session summary: {e}")
        return None

    def format_session_summary(self, session_summary: Optional[Dict]) -> str:
        """Formats the summary of the last completed session (Race, Qualifying, or Practice)."""
        if not session_summary:
            return "❌ <b>Last Race Results Unavailable</b>\n━━━━━━━━━━━━━━━━━━━━\n<i>Could not fetch results for the last completed F1 race.</i>"

        meeting_name = session_summary.get('meeting_name', 'Meeting')
        session_name = session_summary.get('session_name', 'Session')
        top_3 = session_summary.get('top_3', [])
        fastest_lap_data = session_summary.get('fastest_lap', {})
        is_race = session_summary.get('is_race', False)

        session_type_emoji = "🏁" if is_race else ("⏱️" if 'QUALIFYING' in session_name.upper() else "⚙️")
        
        lines = [
            f"{session_type_emoji} <b>Last Race Results: {meeting_name}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        
        for entry in top_3:
            pos = int(entry['position'])
            pos_emoji = "🥇" if pos == 1 else ("🥈" if pos == 2 else "🥉")
            # Display format: P1 (Race) or 1st (P/Q)
            pos_display = f"P{pos}" if is_race else (f"{pos}st" if pos==1 else (f"{pos}nd" if pos==2 else f"{pos}rd"))
            
            lines.append(f"{pos_emoji} <b>{pos_display}</b>: {entry['driver']}")

        lines.append("---")
        
        fl_driver = fastest_lap_data['driver']
        fl_time = fastest_lap_data['time']
        
        lines.append(f"🟣 <b>Best Lap:</b> {fl_driver} ({fl_time})")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━\n<i>Source: Jolpica-F1 (Ergast)</i>")
        
        return "\n".join(lines)


    # --- OpenF1 Session Methods ---

    async def get_latest_session(self) -> Optional[Dict]:
        """
        Get the most recent F1 session, returning cached data if available.
        """
        if self.session_info_cache and self.last_session_fetch and \
           (datetime.now() - self.last_session_fetch).total_seconds() < 300:
            return self.session_info_cache

        try:
            url = f"{OPENF1_API}/sessions"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    sessions = await resp.json()
                    if sessions:
                        sessions.sort(key=lambda x: x.get('date_start', ''), reverse=True)
                        latest_session = sessions[0]
                        self.session_info_cache = latest_session
                        self.last_session_fetch = datetime.now()
                        return latest_session
        except Exception as e:
            logger.error(f"Error fetching session: {e}")
        return None
    
    async def get_weekend_sessions(self, meeting_key: int) -> List[Dict]:
        """Fetches all sessions for a given meeting key."""
        try:
            url = f"{OPENF1_API}/sessions?meeting_key={meeting_key}&order_by=date_start"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    sessions = await resp.json()
                    return sessions
        except Exception as e:
            logger.error(f"Error fetching weekend sessions: {e}")
        return []

    async def get_weekend_sessions_calendar(self) -> Optional[Dict]:
        """Fetch the next upcoming race weekend using the community calendar JSON and return meeting info + sessions.

        Returns a dict with keys: meeting_info (dict) and sessions (List[Dict]) or None if not found.
        """
        try:
            # Dynamically determine the calendar URL for the current year
            current_year = date.today().year
            calendar_url = F1_CALENDAR_API_TEMPLATE.format(year=current_year)
            # Return cached calendar result if still fresh (1 hour)
            if self.calendar_cache and self.last_calendar_fetch and \
               (datetime.now() - self.last_calendar_fetch).total_seconds() < 3600:
                return self.calendar_cache

            if not self._session or not isinstance(self._session, aiohttp.ClientSession):
                # ensure session exists
                await self.init_session()

            async with self.session.get(calendar_url) as resp:
                if resp.status != 200:
                    logger.warning(f"Calendar API returned status {resp.status}")
                    return None
                # GitHub raw may return text/plain, parse explicitly
                text = await resp.text()
                try:
                    calendar = json.loads(text)
                except Exception:
                    logger.exception("Failed to parse calendar JSON")
                    return None

                races = calendar.get('races', [])
                if not races:
                    return None

                now = datetime.now(timezone.utc)
                upcoming = None
                for race in races:
                    sessions = race.get('sessions', {})
                    gp = sessions.get('gp')
                    if not gp:
                        continue
                    gp_clean = gp.strip()
                    try:
                        gp_dt = datetime.strptime(gp_clean, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    except Exception:
                        logger.warning(f"Could not parse gp time for race {race.get('name')}: {gp}")
                        continue
                    if gp_dt > now:
                        upcoming = race
                        break

                if not upcoming:
                    return None

                # Build meeting_info in the shape expected by format_schedule
                meeting_info = {
                    'meeting_name': f"{upcoming.get('name', 'Unknown')} Grand Prix",
                    'circuit_short_name': upcoming.get('localeKey', '').replace('-', ' ').title(),
                    'country_name': upcoming.get('location', 'Unknown')
                }

                # Build sessions list
                session_map = {
                    'fp1': 'Practice 1', 'fp2': 'Practice 2', 'fp3': 'Practice 3',
                    'qualifying': 'Qualifying', 'sprint': 'Sprint Race', 'gp': 'Race',
                    'sprintQualifying': 'Sprint Qualifying'
                }
                formatted = []
                for key, name in session_map.items():
                    if key in upcoming.get('sessions', {}):
                        ts = upcoming['sessions'][key].strip()
                        formatted.append({'session_name': name, 'date_start': ts})

                result = {'meeting_info': meeting_info, 'sessions': sorted(formatted, key=lambda x: x.get('date_start', ''))}
                # cache result
                self.calendar_cache = result
                self.last_calendar_fetch = datetime.now()
                return result
        except Exception as e:
            logger.exception(f"Error fetching weekend from calendar: {e}")
            return None

    async def get_openf1_session_from_jolpica(self, jolpica_meeting: Dict) -> Optional[Dict]:
        """
        Given a meeting from Jolpica, find the corresponding session in OpenF1 to get a meeting_key.
        """
        year = jolpica_meeting.get('season')
        round_number = jolpica_meeting.get('round')

        if not year or not round_number:
            return None

        try:
            # We query OpenF1 for a session matching the year and round number.
            # We only need one session (like Practice 1) to get the meeting_key. OpenF1 uses 'round_number'.
            url = f"{OPENF1_API}/sessions?year={year}&round_number={round_number}&session_name=Practice 1"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    sessions = await resp.json()
                    return sessions[0] if sessions else None
        except Exception as e:
            logger.error(f"Error cross-referencing Jolpica meeting with OpenF1: {e}")
        return None

    # --- Other OpenF1/Formatting methods (truncated for brevity, assume they remain) ---

    async def get_historical_circuit_results(self, circuit_id: str) -> Dict:
        """Fetches winners, pole position, and fastest lap from the last 3 races."""
        # This implementation remains the same, relying on Jolpica circuit ID
        if circuit_id in self.historical_data_cache and \
           self.last_session_fetch and (datetime.now() - self.last_session_fetch).total_seconds() < 3600:
            return self.historical_data_cache[circuit_id]

        history = {
            'winners': [],
            'pole_positions': [],
            'fastest_laps': None, 
            'last_pole_time': 'N/A'
        }
        
        # Helper to convert time string to seconds
        def time_to_seconds(time_str):
            try:
                m, s = map(float, time_str.split(':'))
                return m * 60 + s
            except:
                return float('inf')

        try:
            # 1. Get the last 3 race results
            url = f"{JOLPICA_API}circuits/{circuit_id}/results/1.json?limit=3"
            logger.info(f"Fetching race results from: {url}")
            async with self.session.get(url) as resp:
                logger.info(f"Historical data API status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    races = data.get('MRData', {}).get('RaceTable', {}).get('Races', [])
                    logger.info(f"Found {len(races)} historical races for {circuit_id}")
                    
                    all_fastest_laps = []

                    for race in races:
                        race_year = race.get('season')
                        results = race.get('Results', [])
                        
                        # --- Winners ---
                        if results:
                            winner_driver = results[0].get('Driver', {})
                            winner_abbr = winner_driver.get('code', 'N/A')
                            history['winners'].append(f"{winner_abbr} ({race_year})")
                            
                            # --- Fastest Lap (of that race) ---
                            race_fastest_lap = next((res for res in results if res.get('FastestLap', {}).get('rank') == '1'), None)
                            
                            if race_fastest_lap:
                                fl_driver = race_fastest_lap.get('Driver', {})
                                fl_driver_abbr = fl_driver.get('code', 'N/A')
                                fl_time = race_fastest_lap.get('FastestLap', {}).get('Time', {}).get('time', 'N/A')
                                
                                if fl_time != 'N/A':
                                    all_fastest_laps.append({
                                        'driver_abbr': fl_driver_abbr,
                                        'time': fl_time
                                    })
                
                    if all_fastest_laps:
                        history['fastest_laps'] = min(all_fastest_laps, key=lambda x: time_to_seconds(x['time']))


            # 2. Get the last Pole Position
            url_races_single = f"{JOLPICA_API}circuits/{circuit_id}/results/1.json?limit=1"
            async with self.session.get(url_races_single) as resp:
                if resp.status != 200: 
                    logger.warning(f"Failed to get qualifying data: {resp.status}")
                    self.historical_data_cache[circuit_id] = history
                    return history

                data_races = await resp.json()
                races = data_races.get('MRData', {}).get('RaceTable', {}).get('Races', [])
                
                if races:
                    last_race = races[0]
                    year = last_race['season']
                    race_round = last_race['round']

                    url_qualifying = f"{JOLPICA_API}{year}/{race_round}/qualifying/1.json"
                    logger.info(f"Fetching qualifying from: {url_qualifying}")
                    async with self.session.get(url_qualifying) as resp_q:
                        if resp_q.status == 200:
                            data_q = await resp_q.json()
                            qualifying_results = data_q.get('MRData', {}).get('RaceTable', {}).get('Races', [{}])[0].get('QualifyingResults', [])
                            logger.info(f"Found {len(qualifying_results)} qualifying results")
                            
                            if qualifying_results and qualifying_results[0].get('position') == '1':
                                pole_data = qualifying_results[0]
                                driver = pole_data.get('Driver', {})
                                lap_time = pole_data.get('Q3') or pole_data.get('Q2') or pole_data.get('Q1')
                                
                                if not lap_time:
                                    lap_time = pole_data.get('Time', {}).get('time', 'N/A')
                                
                                history['pole_positions'] = [f"{driver.get('code', 'N/A')} ({year})"]
                                history['last_pole_time'] = lap_time
        
        except Exception as e:
            logger.error(f"Error fetching historical circuit data: {e}")
        
        self.historical_data_cache[circuit_id] = history
        return history

    def format_schedule(self, sessions: List[Dict], meeting_info: Dict) -> str:
        """
        Format the weekend schedule, converting UTC times to the event's local time 
        (based on DEFAULT_LOCAL_TIME_OFFSET), using 24-hour format without offset display.
        """
        header_meeting_name = meeting_info.get('meeting_name', 'F1 Weekend')
        circuit_name = meeting_info.get('circuit_short_name') or meeting_info.get('Circuit', {}).get('circuitName', 'Track')
        country_name = meeting_info.get('country_name') or meeting_info.get('Circuit', {}).get('Location', {}).get('country', 'Location')
        # Use provided timezone offset (default: Baku UTC+4)
        tz_offset = meeting_info.get('tz_offset', BAKU_TIME_OFFSET)
        def lookup_flag(name: Optional[str]) -> Optional[str]:
            if not name:
                return None
            for k, v in FLAG_EMOJIS.items():
                if k.lower() in name.lower() or name.lower() in k.lower():
                    return v
            return None

        flag_emoji = lookup_flag(country_name) or lookup_flag(meeting_info.get('meeting_name')) or '🏳️'

        lines = [
            f"📅 <b>{header_meeting_name}</b>",
            f"📍 {flag_emoji} {circuit_name}, {country_name}",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        
        if not sessions:
            lines.append("<i>Schedule data not available.</i>")
            return "\n".join(lines)

        for session in sessions:
            session_name = session.get('session_name', 'N/A')
            start_date_utc_str = session.get('date_start')
            
            try:
                if start_date_utc_str and isinstance(start_date_utc_str, str):
                    start_date_utc = datetime.fromisoformat(start_date_utc_str.replace('Z', '+00:00'))
                    # Apply the provided offset by adding tz_offset
                    start_date_local = start_date_utc + tz_offset
                    # Format: Day, Date Month | HH:MM (24h)
                    formatted_time = start_date_local.strftime('%a, %d %b | %H:%M')
                else:
                    formatted_time = 'Time N/A'
                lines.append(f"• {session_name}: <b>{formatted_time}</b>")
                
            except (ValueError, TypeError):
                lines.append(f"• {session_name}: <i>Time N/A</i>")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        # Build a friendly footer describing the timezone used
        try:
            if tz_offset == BAKU_TIME_OFFSET:
                tz_note = "Baku local time (UTC+4)"
            elif tz_offset == timedelta(0):
                tz_note = "UTC"
            else:
                hours = int(tz_offset.total_seconds() // 3600)
                sign = '+' if hours >= 0 else '-'
                tz_note = f"UTC{sign}{abs(hours)}"
        except Exception:
            tz_note = "local time"

        lines.append(f"<i>All times are in {tz_note}.</i>")
        
        return "\n".join(lines)

    # --- OpenF1 Live Timing Methods ---

    async def get_live_timing(self, session_key: int) -> List[Dict]:
        """Fetches the latest position and status for all drivers in the session."""
        try:
            url = f"{OPENF1_API}/position?session_key={session_key}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    positions = await resp.json()
                    driver_positions = {}
                    for pos in positions:
                        driver_num = pos.get('driver_number')
                        if driver_num and driver_num not in driver_positions:
                            driver_positions[driver_num] = pos

                    sorted_positions = sorted(
                        driver_positions.values(),
                        key=lambda x: x.get('position', 999)
                    )
                    return sorted_positions
        except Exception as e:
            logger.error(f"Error fetching live timing: {e}")
        return []

    async def get_lap_times(self, session_key: int) -> Dict[int, str]:
        """Get the latest completed lap time for each driver."""
        # Implementation remains the same
        try:
            url = f"{OPENF1_API}/laps?session_key={session_key}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    laps = await resp.json()
                    driver_laps = {}
                    for lap in laps:
                        driver_num = lap.get('driver_number')
                        lap_duration = lap.get('lap_duration')
                        if driver_num and lap_duration and driver_num not in driver_laps:
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
        # Implementation remains the same
        try:
            url = f"{OPENF1_API}/stints?session_key={session_key}"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    stints = await resp.json()
                    driver_stints = {}
                    for stint in stints:
                        driver_num = stint.get('driver_number')
                        compound = stint.get('compound', 'UNKNOWN')
                        if driver_num and driver_num not in driver_stints:
                            driver_stints[driver_num] = compound.upper()
                    return driver_stints
        except Exception as e:
            logger.error(f"Error fetching stints: {e}")
        return {}

    async def get_intervals(self, session_key: int) -> Dict[int, str]:
        """Get the latest intervals (gaps) for each driver."""
        if (not self.intervals_cache.get(session_key) or 
            not self.last_intervals_fetch or 
            (datetime.now() - self.last_intervals_fetch).total_seconds() > 15):
            try:
                url = f"{OPENF1_API}/intervals?session_key={session_key}"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        intervals = await resp.json()
                        driver_intervals = {}
                        for inter in intervals:
                            driver_num = inter.get('driver_number')
                            gap = inter.get('gap_to_leader')
                            if driver_num and gap and driver_num not in driver_intervals:
                                driver_intervals[driver_num] = gap
                        self.intervals_cache[session_key] = driver_intervals
                        self.last_intervals_fetch = datetime.now()
            except Exception as e:
                logger.error(f"Error fetching intervals: {e}")
                return {}
        return self.intervals_cache.get(session_key, {})

    async def get_fastest_lap(self, session_key: int) -> Optional[Dict]:
        """Determines the current fastest lap based on lap times."""
        # Implementation remains the same
        try:
            lap_times = await self.get_lap_times(session_key)
            if not lap_times:
                return None
                
            def time_to_seconds(time_str):
                try:
                    m, s = map(float, time_str.split(':'))
                    return m * 60 + s
                except:
                    return 99999.0
                    
            valid_times = [time_to_seconds(t) for t in lap_times.values() if time_to_seconds(t) < 99999.0]
            if not valid_times:
                return None
                
            fastest_time_seconds = min(valid_times)
            
            fastest_lap_driver_num = next(
                (d_num for d_num, t_str in lap_times.items() 
                 if math.isclose(time_to_seconds(t_str), fastest_time_seconds, rel_tol=1e-9)),
                None
            )
            
            if fastest_lap_driver_num:
                return {
                    'driver_number': fastest_lap_driver_num,
                    'lap_time': lap_times[fastest_lap_driver_num]
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
        intervals: Dict[int, str] = None  # New: real gaps
    ) -> str:
        """Format the live dashboard message with real gaps and all drivers."""
        # Get values with safe fallbacks
        race_name = session_info.get('meeting_name', 'F1 Race')
        circuit_name = session_info.get('circuit_short_name', 'Track')
        session_name = session_info.get('session_name', 'Session')

        lines = [
            f"📍 <b>{race_name} ({circuit_name})</b>", 
            "🏎️ <b>F1 Live Dashboard</b>",
            f"🏁 {session_name}",
        ]

        driver_abbr = self.dynamic_driver_abbr
        
        fastest_lap_driver_num = None
        if fastest_lap_data:
            driver_number = fastest_lap_data.get('driver_number')
            if driver_number is not None:
                fastest_lap_driver_num = driver_number
                # Safe access for driver abbreviation
                fl_abbr = f"DR{driver_number}"
                if driver_number in driver_abbr:
                    fl_abbr = driver_abbr[driver_number]
                fl_time = fastest_lap_data.get('lap_time', '---')
                lines.append(f"🟣 <b>Fastest Lap:</b> {fl_abbr} ({fl_time})")
        
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        # Determine current lap from positions if available
        current_lap = None
        try:
            laps_found = [
                int(p.get('lap') or p.get('laps') or p.get('lap_number') or p.get('current_lap') or 0)
                for p in positions if isinstance(p, dict)
            ] if positions else []
            if laps_found:
                current_lap = max(laps_found)
        except Exception:
            current_lap = None

        # Try to read total laps from session_info if present
        total_laps = None
        for k in ('lap_count', 'laps', 'race_laps', 'total_laps'):
            try:
                v = session_info.get(k)
                if v:
                    total_laps = int(v)
                    break
            except Exception:
                continue

        # If we have lap info, display it above the grid
        if current_lap is not None:
            if total_laps:
                lines.append(f"🏁 <i>Lap {current_lap} / {total_laps}</i>")
            else:
                lines.append(f"🏁 <i>Lap {current_lap}</i>")

        if not positions:
            lines.append("\n⏳ <i>Waiting for live data (OpenF1 data is often sparse)...</i>")
            return "\n".join(lines)

        for pos_data in positions:
            driver_num = pos_data.get('driver_number')
            if driver_num is None:
                continue

            position = pos_data.get('position')
            if position is None:
                position = '?'
                
            # Safe dictionary access for driver abbreviation
            driver_abbr_code = f"DR{driver_num}"
            if driver_num in driver_abbr:
                driver_abbr_code = driver_abbr[driver_num]
            
            # Handle position display
            try:
                pos_num = int(position) if isinstance(position, (int, str)) else 0
                # Show podium emojis only after session is finished (not while live)
                is_live_flag = self.is_session_live(session_info)
                if not is_live_flag:
                    pos_emoji = "🥇" if pos_num == 1 else ("🥈" if pos_num == 2 else ("🥉" if pos_num == 3 else "  "))
                else:
                    pos_emoji = "  "
            except (ValueError, TypeError):
                pos_emoji = "  "

            # Use real gap from intervals if available, else fallback
            gap = "+?.???"
            if intervals and driver_num in intervals:
                gap = intervals[driver_num]
                if isinstance(gap, (int, float)):
                    gap = f"+{float(gap):.3f}"
                elif 'LAP' in str(gap).upper():
                    gap = str(gap)  # e.g., +1 LAP
            elif pos_num > 1:
                gap = f"+{pos_num * 0.5:.3f}"  # Old fallback

            # Safe dictionary access for lap time
            lap_time = "-:--.---"
            if driver_num in lap_times:
                lap_time = lap_times[driver_num]
                if isinstance(lap_time, str) and len(lap_time) > 10:
                    lap_time = lap_time[:10]

            # Per-driver current lap if available
            try:
                driver_lap = pos_data.get('lap') or pos_data.get('laps') or pos_data.get('lap_number') or pos_data.get('current_lap')
                if driver_lap is None:
                    driver_lap_str = ''
                else:
                    driver_lap_str = f" | L:{int(driver_lap)}"
            except Exception:
                driver_lap_str = ''

            # Safe dictionary access for tyres
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
        lines.append(f"🔄 <i>Updated: {datetime.now(baku_tz).strftime('%H:%M:%S Baku Time')}</i>")

        return "\n".join(lines)

    def format_race_control(self, messages: List[Dict]) -> str:
        """Format the Race Control messages."""
        # Implementation remains the same
        if not messages:
            return "⚠️ <b>Race Control</b>\n━━━━━━━━━━━━━━━━\n<i>No recent messages</i>"

        lines = [
            "⚠️ <b>Race Control Messages</b>",
            "━━━━━━━━━━━━━━━━━━━━"
        ]

        for msg in messages[:5]:
            category = msg.get('category', 'Info')
            message = msg.get('message', '')
            flag = msg.get('flag', '')

            flag_emoji = ""
            if "YELLOW" in flag.upper():
                flag_emoji = "🟨"
            elif "RED" in flag.upper():
                flag_emoji = "🟥"
            elif "GREEN" in flag.upper():
                flag_emoji = "🟩"
            elif "CHEQUERED" in flag.upper() or "CHECKERED" in flag.upper():
                flag_emoji = "🏁"

            time_str = msg.get('date', '')[:19].replace('T', ' ') if msg.get('date') else ''

            lines.append(f"{flag_emoji} <b>{category}</b>: {message}")
            if time_str:
                lines.append(f"   <i>{time_str} UTC</i>")
        
        return "\n".join(lines)


# Global dashboard instance
dashboard = F1LiveDashboard()

async def check_for_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """Job queue wrapper to trigger session alerts."""
    await dashboard.init_session()
    # Alerting system code placeholder...
    # await dashboard.alert_upcoming_sessions(context)


from message_utils import get_message, get_chat_id, safe_reply

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    await dashboard.init_session()
    chat_id = get_chat_id(update)
    if not chat_id:
        return
    dashboard.subscribed_chats.add(chat_id)
    # Build keyboard
    keyboard = [
        [InlineKeyboardButton("🔴 Start Live Updates", callback_data="live")],
        [InlineKeyboardButton("🏆 Standings", callback_data="standings"),
         InlineKeyboardButton("💬 Commentary", callback_data="commentary")],
        [InlineKeyboardButton("▶️ Demo (Simulate)", callback_data="demo"), InlineKeyboardButton("🔔 Auto Commentary", callback_data="autocomm")],
        [InlineKeyboardButton("🗓️ Weekend Schedule", callback_data="schedule")],
        [InlineKeyboardButton("➕ Add to Group", callback_data="add")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message = get_message(update)
    if message:
        await safe_reply(update, "🏎️ <b>F1 Live Dashboard</b>\n\nWelcome! Use the buttons below to get live F1 data.", parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    help_text = (
        "📖 <b>F1 Live Dashboard - Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start - Show main menu\n"
        "/live - Start live timing updates\n"
        "/stop - Stop live updates\n"
        "/commentary - View recent events and start live commentary\n"
        "/standings - View current driver championship standings\n"
        "/schedule [tz] - View weekend schedule (e.g., `/schedule utc-5`)\n"
        "\n<b>Data Sources:</b>\n"
        "• **Live Timing/Weather/Circuit:** OpenF1\n"
        "• **Lineup/Standings/History (Reliable):** Jolpica-F1\n"
    )

    await safe_reply(update, help_text)


async def update_dashboard_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, session_key: int):
    """Background task to update the dashboard with real gaps and all drivers."""
    try:
        # Add a small initial delay to allow users to see the "Live data started" message.
        await asyncio.sleep(3)

        while True:
            try:
                # Fetch all data
                session_info = await dashboard.get_latest_session()
                # If session finished while loop was running, cancel task
                if not dashboard.is_session_live(session_info):
                    raise asyncio.CancelledError("Session finished, stopping live updates.")
                    
                positions = await dashboard.get_live_timing(session_key)
                lap_times = await dashboard.get_lap_times(session_key)
                tyres = await dashboard.get_stints(session_key)
                intervals = await dashboard.get_intervals(session_key)  # New: fetch real gaps
                fastest_lap_data = await dashboard.get_fastest_lap(session_key)
                favorite = dashboard.user_favorites.get(chat_id)

                if session_info:
                    text = dashboard.format_dashboard(
                        session_info, positions, lap_times, tyres, favorite, fastest_lap_data, intervals
                    )

                    keyboard = [
                        [InlineKeyboardButton("⏹️ Stop Updates", callback_data="stop"),
                         InlineKeyboardButton("💬 Commentary", callback_data="commentary")],
                        [InlineKeyboardButton("🗓️ Weekend Schedule", callback_data="schedule")],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )

                await asyncio.sleep(15)  # Update every 15 seconds

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in update loop: {e}")
                await asyncio.sleep(30) 

    except asyncio.CancelledError:
        logger.info(f"Update task cancelled for chat {chat_id}")
        # When cancelled, revert the message to a static one (best effort)
        static_message = "⏹️ Live updates for the session have ended."
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=static_message,
            parse_mode=ParseMode.HTML
        )


async def commentary_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, session_key: int):
    """Background task: periodically fetch race control and session events and send new items as messages."""
    try:
        # Ensure seen set exists
        # CRITICAL: Ensure driver names are loaded for commentary
        await dashboard.get_live_driver_lineup()
        
        dashboard.commentary_seen.setdefault(chat_id, set())

        while True:
            try:
                rc_msgs = await dashboard.get_race_control(session_key)
                events = await dashboard.get_session_events(session_key)

                # Process race control messages (new ones only)
                for msg in reversed(rc_msgs):
                    try:
                        key = json.dumps(msg, sort_keys=True)
                    except Exception:
                        key = str(msg)
                    if key in dashboard.commentary_seen[chat_id]:
                        continue
                    dashboard.commentary_seen[chat_id].add(key)

                    # Format: title/body or full JSON fallback
                    title = msg.get('title') or msg.get('type') or 'Race Control'
                    body = msg.get('message') or msg.get('description') or msg.get('text') or str(msg)
                    text = f"⚠️ <b>{title}</b>\n{body}"
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

                # Process other events (overtakes, pit stops, DNF, etc.)
                for ev in events:
                    try:
                        key = json.dumps(ev, sort_keys=True)
                    except Exception:
                        key = str(ev)
                    if key in dashboard.commentary_seen[chat_id]:
                        continue
                    dashboard.commentary_seen[chat_id].add(key)

                    ev_type = (ev.get('type') or ev.get('event') or '').strip()
                    ev_text = (ev.get('description') or ev.get('short_text') or ev.get('message') or str(ev)).strip()

                    # Helper to map numeric driver to 3-letter code
                    def code_for_num(n: int) -> str:
                        try:
                            num = int(n)
                            return dashboard.dynamic_driver_abbr.get(num, f"DR{num}")
                        except (ValueError, TypeError):
                            return str(n) # Return original string if not a number
                        except Exception:
                            return f"DR{n}"

                    # Helper to find lap in text or event fields
                    def find_lap(text: str, ev: Dict) -> Optional[int]:
                        for k in ('lap', 'lap_number', 'laps', 'current_lap'):
                            v = ev.get(k)
                            if isinstance(v, (int, str)):
                                try:
                                    return int(v)
                                except Exception:
                                    continue
                        m = re.search(r"lap(?:s)?\s*(?:#?:)?\s*(\d{1,3})", text, flags=re.IGNORECASE)
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

                    # Detect overtakes
                    if 'overtake' in ev_type.lower() or 'overtook' in ev_text.lower() or 'overtake' in ev_text.lower():
                        # Try structured fields
                        a = ev.get('overtaker') or ev.get('attacker') or ev.get('from') or ev.get('driver') or ev.get('subject')
                        b = ev.get('overtaken') or ev.get('defender') or ev.get('to') or ev.get('target')

                        # If structured numeric driver ids are present
                        try:
                            if isinstance(a, (int, str)) and isinstance(b, (int, str)):
                                a_code = code_for_num(a)
                                b_code = code_for_num(b)
                                lapn = find_lap(ev_text, ev)
                                if lapn:
                                    formatted = f"🔁 <b>Overtake (L{lapn})</b>\n{a_code} overtook {b_code}"
                                else:
                                    formatted = f"🔁 <b>Overtake</b>\n{a_code} overtook {b_code}"
                        except Exception:
                            formatted = None

                        if not formatted:
                            # Try regex like DR11 overtook DR16 or 11 overtook 16
                            m = re.search(r"DR?(\d{1,2})\s*overtook\s*DR?(\d{1,2})", ev_text, flags=re.IGNORECASE)
                            if m:
                                a_code = code_for_num(int(m.group(1)))
                                b_code = code_for_num(int(m.group(2)))
                                lapn = find_lap(ev_text, ev)
                                if lapn:
                                    formatted = f"🔁 <b>Overtake (L{lapn})</b>\n{a_code} overtook {b_code}"
                                else:
                                    formatted = f"🔁 <b>Overtake</b>\n{a_code} overtook {b_code}"

                    # Detect pit stops
                    if not formatted and ('pit' in ev_type.lower() or 'pit' in ev_text.lower() or 'stop' in ev_type.lower() and 'pit' in ev_text.lower()):
                        # try driver
                        dn = ev.get('driver_number') or ev.get('driver') or ev.get('car')
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
                        detail = ev_text
                        if code:
                            formatted = f"🛠️ <b>Pit Stop</b>\n{code} pitted — {detail}"
                        else:
                            formatted = f"🛠️ <b>Pit Stop</b>\n{detail}"

                    # Detect retirements / DNF
                    if not formatted and ('dnf' in ev_type.lower() or 'retir' in ev_type.lower() or 'retire' in ev_text.lower() or 'retired' in ev_text.lower()):
                        dn = ev.get('driver_number') or ev.get('driver')
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

                    # Fallback: replace DRNN tokens with codes and show raw text
                    if not formatted:
                        try:
                            ev_text = re.sub(r"DR(\d{1,2})", lambda m: code_for_num(int(m.group(1))), ev_text)
                        except Exception:
                            pass
                        formatted = f"ℹ️ <b>{ev_type or 'Event'}</b>\n{ev_text}"

                    await context.bot.send_message(chat_id=chat_id, text=formatted, parse_mode=ParseMode.HTML)

                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in commentary loop for chat {chat_id}: {e}")
                await asyncio.sleep(15)
    except asyncio.CancelledError:
        logger.info(f"Commentary task cancelled for chat {chat_id}")


async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start live timing updates or show last session summary if not live."""
    await dashboard.init_session()

    chat_id = get_chat_id(update)
    if not chat_id:
        return
    
    dashboard.subscribed_chats.add(chat_id)

    # CRITICAL: Fetch the current lineup first
    await dashboard.get_live_driver_lineup()

    # Use the session that was last active
    session = await dashboard.get_latest_session()

    if not session:
        await safe_reply(update, "❌ No recent F1 session found. Please try again later.")
        return

    # 1. Determine if the session is currently live
    is_live = dashboard.is_session_live(session)
    
    if is_live:
        # --- LOGIC FOR LIVE SESSION ---
        session_key = session.get('session_key')
        dashboard.current_session_key = session_key

        # Cancel previous task if one exists
        if chat_id in dashboard.update_tasks:
            dashboard.update_tasks[chat_id].cancel()

        initial_text = (
            "🏎️ <b>F1 Live Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Session: {session.get('meeting_name')}, {session.get('session_name')}\n"
            "🟢 <i>Live data stream started.</i>"
        )

        # Attach control keyboard immediately so users see buttons before the live edit starts
        init_keyboard = [
            [InlineKeyboardButton("⏹️ Stop Updates", callback_data="stop"),
             InlineKeyboardButton("💬 Commentary", callback_data="commentary")],
            [InlineKeyboardButton("🗓️ Weekend Schedule", callback_data="schedule")],
            [InlineKeyboardButton("➕ Add to Group", callback_data="add")]
        ]
        init_reply = InlineKeyboardMarkup(init_keyboard)
        sent_message = await safe_reply(update, initial_text, parse_mode=ParseMode.HTML, reply_markup=init_reply)
        if not sent_message:
            return

        dashboard.live_messages[chat_id] = sent_message.message_id

        # Get session key with validation
        session_key = session.get('session_key') if session else None
        if session_key is not None and isinstance(session_key, int):
            # Create new task
            task = asyncio.create_task(
                update_dashboard_loop(context, chat_id, sent_message.message_id, session_key)
            )
            dashboard.update_tasks[chat_id] = task
            # If this chat has auto-commentary enabled, start commentary loop as well
            try:
                ac = getattr(dashboard, 'auto_commentary', set())
                if chat_id in ac and chat_id not in dashboard.commentary_tasks:
                    ctask = asyncio.create_task(commentary_loop(context, chat_id, session_key))
                    dashboard.commentary_tasks[chat_id] = ctask
                    await safe_reply(update, "🔔 Auto-commentary is enabled: started commentary for this live session.")
            except Exception as e:
                logger.error(f"Error starting auto-commentary: {e}")
        else:
            await safe_reply(update, "❌ Error: Invalid session key")

    else:
        # No live session: show last race results and offer other actions
        await safe_reply(update, "⏳ No live session right now. Fetching last race results...")
        last_race_summary = await dashboard.get_last_race_results_summary()
        
        if last_race_summary:
            summary_text = dashboard.format_session_summary(last_race_summary)
        else:
            summary_text = "Could not fetch last race results."

        full_message = f"⚠️ There is no live session right now.\n\n{summary_text}"

        keyboard = [
            [InlineKeyboardButton("🗓️ Weekend Schedule", callback_data="schedule"), InlineKeyboardButton("🏆 Standings", callback_data="standings")],
            [InlineKeyboardButton("▶️ Demo (Simulate)", callback_data="demo"), InlineKeyboardButton("❓ Help", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await safe_reply(update, full_message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop live timing updates"""
    chat_id = get_chat_id(update)
    if not chat_id:
        return

    if chat_id in dashboard.update_tasks:
        dashboard.update_tasks[chat_id].cancel()
        del dashboard.update_tasks[chat_id]
        # Also stop commentary if running
        if chat_id in dashboard.commentary_tasks:
            dashboard.commentary_tasks[chat_id].cancel()
            del dashboard.commentary_tasks[chat_id]
            dashboard.commentary_seen.pop(chat_id, None)
        await safe_reply(update, "⏹️ Live updates stopped.")
    else:
        await safe_reply(update, "ℹ️ No active live updates.")


async def simulate_live_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Simulated live loop using FastF1 historical data for demo/testing."""
    # CRITICAL: Ensure driver names are loaded before starting simulation
    await dashboard.get_live_driver_lineup()

    try:
        # Load a historical session (e.g., Mexico 2025 Race) - run synchronously in executor
        year = 2025
        gp = 'Mexico'
        session_type = 'R'
        session = await asyncio.get_event_loop().run_in_executor(None, lambda: fastf1.get_session(year, gp, session_type))
        await asyncio.get_event_loop().run_in_executor(None, lambda: session.load(telemetry=True, laps=True, weather=True))

        laps = session.laps
        max_laps = int(laps['LapNumber'].max())

        # Function to format lap time as MM:SS.mmm
        def format_lap_time(lap_time):
            if pd.isna(lap_time):
                return '1:31.000'  # Fallback
            seconds = lap_time.total_seconds()
            minutes = int(seconds // 60)
            seconds = seconds % 60
            return f"{minutes}:{seconds:06.3f}"

        for lap in range(1, max_laps + 1):
            # Get lap data for current lap
            current_laps = laps[laps['LapNumber'] == lap]
            if current_laps.empty:
                continue

            # Sort by position
            current_laps = current_laps.sort_values('Position')
            if current_laps['Position'].isnull().all():
                current_laps = current_laps.sort_values('LapTime')  # Fallback

            # Build positions list for format_dashboard
            positions = []
            for idx, row in current_laps.iterrows():
                driver_num = int(row['DriverNumber']) if pd.notnull(row['DriverNumber']) else idx + 1
                position = int(row['Position']) if pd.notnull(row['Position']) else idx + 1
                positions.append({
                    'driver_number': driver_num,
                    'position': position
                })

            # Lap times dict
            lap_times = {}
            for idx, row in current_laps.iterrows():
                driver_num = int(row['DriverNumber']) if pd.notnull(row['DriverNumber']) else idx + 1
                lap_time = row['LapTime']
                lap_times[driver_num] = format_lap_time(lap_time)

            # Tyres dict
            tyres = {}
            for idx, row in current_laps.iterrows():
                driver_num = int(row['DriverNumber']) if pd.notnull(row['DriverNumber']) else idx + 1
                compound = row.get('Compound', 'UNKNOWN').upper()
                tyres[driver_num] = compound

            # Leader's lap time for gaps
            leader_lap = current_laps.iloc[0]['LapTime'] if not current_laps.empty else pd.Timedelta(0)

            # Intervals (gaps) dict
            intervals = {}
            for idx, row in current_laps.iterrows():
                driver_num = int(row['DriverNumber']) if pd.notnull(row['DriverNumber']) else idx + 1
                lap_time = row['LapTime']
                gap_sec = (lap_time - leader_lap).total_seconds() if pd.notnull(lap_time) else 0.0
                intervals[driver_num] = f"{gap_sec:.3f}"

            # Fastest lap (for the whole session up to now)
            fastest_lap_data = await dashboard.get_fastest_lap(session_key=None)  # Placeholder, or calculate from laps

            # Session info for demo
            session_info = {
                'meeting_name': 'Demo Replay - Mexico 2025',
                'circuit_short_name': 'Mexico City',
                'session_name': 'Race'
            }

            text = dashboard.format_dashboard(
                session_info, positions, lap_times, tyres, None, fastest_lap_data, intervals
            )

            keyboard = [
                [InlineKeyboardButton("⏹️ Stop Updates", callback_data="stop"),
                 InlineKeyboardButton("💬 Commentary", callback_data="commentary")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )

            await asyncio.sleep(2)  # Simulate update delay

    except asyncio.CancelledError:
        logger.info(f"Simulation loop cancelled for chat {chat_id}")


async def simulate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a demo simulated live session in the chat. Can be invoked as /simulate or via Demo button."""
    chat_id = get_chat_id(update)
    if not chat_id:
        return

    # If a live task already exists, inform user
    if chat_id in dashboard.update_tasks:
        await safe_reply(update, "⚠️ A live session or demo is already running in this chat. Press Stop to end it.")
        return

    initial_text = (
        "🏁 <b>Demo Live Replay</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "This demo simulates live timing and commentary using historic-style events.\n"
        "Press Stop to end the demo.\n"
    )

    kb = [
        [InlineKeyboardButton("⏹️ Stop Updates", callback_data="stop"), InlineKeyboardButton("💬 Commentary", callback_data="commentary")],
    ]
    reply_markup = InlineKeyboardMarkup(kb)

    sent_message = await safe_reply(update, initial_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    if not sent_message:
        return

    # Create simulation task and register it like a live update
    task = asyncio.create_task(simulate_live_loop(context, chat_id, sent_message.message_id))
    dashboard.update_tasks[chat_id] = task

async def racecontrol_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show race control messages"""
    await dashboard.init_session()

    # Always use the latest session key for context
    session = await dashboard.get_latest_session()
    session_key = session.get('session_key') if session else None

    if not session_key:
        await safe_reply(update, "❌ No active or recent session found.")
        return
        
    is_live = dashboard.is_session_live(session)
    if not is_live:
        await safe_reply(update, "⚠️ Session is over. Race control messages may be outdated or empty.")

    messages = await dashboard.get_race_control(session_key)
    text = dashboard.format_race_control(messages)

    await safe_reply(update, text)


async def commentary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Combined commentary mode: show race control messages and recent session events (overtakes, pit stops, DNF).

    If the session is live, start a background commentary loop that will push new items to the chat.
    """
    await dashboard.init_session()

    chat_id = get_chat_id(update)
    if not chat_id:
        return

    session = await dashboard.get_latest_session()
    session_key = session.get('session_key') if session else None
    if not session_key:
        await safe_reply(update, "❌ No active or recent session found.")
        return

    is_live = dashboard.is_session_live(session)

    # Fetch current snapshot
    rc_messages = await dashboard.get_race_control(session_key)
    events = await dashboard.get_session_events(session_key)

    parts = []
    if rc_messages:
        parts.append(dashboard.format_race_control(rc_messages))
    if events:
        parts.append("🔎 <b>Recent Events</b>\n━━━━━━━━━━━━━━━━━━━━")
        for ev in events[:10]:
            ev_type = ev.get('type') or ev.get('event') or 'Event'
            ev_text = ev.get('description') or ev.get('short_text') or ev.get('message') or str(ev)
            parts.append(f"• <b>{ev_type}</b>: {ev_text}")

    if not parts:
        await safe_reply(update, "ℹ️ No commentary or events available at the moment.")
    else:
        await safe_reply(update, "\n\n".join(parts), parse_mode=ParseMode.HTML)

    # If live and no commentary task for this chat, start one
    if is_live and chat_id not in dashboard.commentary_tasks:
        task = asyncio.create_task(commentary_loop(context, chat_id, session_key))
        dashboard.commentary_tasks[chat_id] = task
        await safe_reply(update, "🔔 Commentary mode started — you'll receive live race-control and event updates.")


async def autocommentary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-commentary preference for this chat.

    When enabled, the bot will automatically start commentary for this chat when a live session begins.
    """
    chat_id = get_chat_id(update)
    if not chat_id:
        return

    ac = getattr(dashboard, 'auto_commentary', set())
    if chat_id in ac:
        ac.remove(chat_id)
        await safe_reply(update, "🔕 Auto-commentary disabled for this chat.")
    else:
        ac.add(chat_id)
        await safe_reply(update, "🔔 Auto-commentary enabled for this chat. I'll start commentary automatically when a live session begins.")
    dashboard.auto_commentary = ac

async def standings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current standings fetched from Jolpica-F1."""
    await dashboard.init_session()

    await dashboard.get_live_driver_lineup()

    standings_data = await dashboard.get_championship_standings()
    text = dashboard.format_standings(standings_data if standings_data else [])

    await safe_reply(update, text, parse_mode=ParseMode.HTML)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the full weekend schedule in the event's local time."""
    await dashboard.init_session()

    # Determine which meeting schedule to show: the current (if upcoming) or the next one.
    meeting_info = None
    
    # Check if a session is live first
    latest_session = await dashboard.get_latest_session()
    is_live = dashboard.is_session_live(latest_session)

    # Always find the next race for the schedule to avoid confusion.
    await safe_reply(update, "⏳ Finding next race weekend schedule...")
    next_meeting = await dashboard.get_next_race_meeting()
    if next_meeting:
        meeting_info = next_meeting # Switch context to the next meeting
    
    if not meeting_info:
        await safe_reply(update, "❌ Could not determine the current or next F1 weekend.")
        return
    
    # Parse optional timezone argument from the command (e.g., /schedule baku or /schedule utc or /schedule utc+3)
    tz_offset = BAKU_TIME_OFFSET
    user_tz = 'baku'
    try:
        args = context.args if context and hasattr(context, 'args') else []
        if args:
            arg = args[0].strip().lower()
            user_tz = arg
            if arg == 'baku':
                tz_offset = BAKU_TIME_OFFSET
            elif arg == 'utc':
                tz_offset = timedelta(hours=0)
            else:
                # support formats like utc+3, utc-2, +3, -1
                m = re.match(r'^(?:utc)?([+-]?\d+)$', arg)
                if m:
                    off_hours = int(m.group(1))
                    tz_offset = timedelta(hours=off_hours)
    except Exception:
        # If parsing fails, default to Baku
        tz_offset = BAKU_TIME_OFFSET
        user_tz = 'baku'

    # If meeting_info is from Jolpica, it won't have a meeting_key. We need to find it.
    if not meeting_info.get('meeting_key'):
        openf1_equivalent_session = await dashboard.get_openf1_session_from_jolpica(meeting_info)
        if openf1_equivalent_session:
            meeting_info['meeting_key'] = openf1_equivalent_session.get('meeting_key')

    meeting_key = meeting_info.get('meeting_key')

    if not meeting_key:
        # Try fallback to community calendar JSON if OpenF1 meeting_key is not available
        cal = await dashboard.get_weekend_sessions_calendar() # Corrected call
        if cal:
            meeting_info_cal = cal.get('meeting_info', {})
            sessions = cal.get('sessions', [])
            # Attach the requested timezone offset for formatting
            meeting_info_cal['tz_offset'] = tz_offset
            text = dashboard.format_schedule(sessions, meeting_info_cal)
            await safe_reply(update, text)
            return
        else:
            # OpenF1 meeting_key is required to fetch all session times for a weekend.
            await safe_reply(update,
                f"❌ Cannot fetch full session schedule for <b>{meeting_info.get('meeting_name')}</b> as OpenF1 meeting data is not yet available. Please try again closer to the event.")
            return

    await safe_reply(update, "⏳ Fetching schedule data...")
    
    sessions = await dashboard.get_weekend_sessions(meeting_key)
    
    # Attach timezone offset for formatting
    meeting_info['tz_offset'] = tz_offset
    text = dashboard.format_schedule(sessions, meeting_info)
    await safe_reply(update, text)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    if not query:
        return
        
    await query.answer()

    callback_handlers = {
        "live": live_command,
        "stop": stop_command,
        "racecontrol": racecontrol_command,
        "commentary": commentary_command,
        "autocomm": autocommentary_command,
        "help": help_command,
        "standings": standings_command,
        "schedule": schedule_command,
        "add": None  # placeholder, handled below
    }

    if query.data and isinstance(query.data, str):
        handler = callback_handlers.get(query.data)
        if handler:
            await handler(update, context)
        elif query.data == 'add':
            # Provide an invite link to add the bot to a group/chat as a URL button
            try:
                bot_user = await context.bot.get_me()
                bot_username = getattr(bot_user, 'username', None)
                if bot_username:
                    url = f"https://t.me/{bot_username}?startgroup=true"
                    kb = [[InlineKeyboardButton("➕ Add to group", url=url)]]
                    reply_markup = InlineKeyboardMarkup(kb)
                    await safe_reply(update, "Tap the button below to add me to a group:", reply_markup=reply_markup)
                else:
                    await safe_reply(update, "Could not determine bot username to create invite link.")
            except Exception as e:
                logger.error(f"Error generating add-to-group link: {e}")
                await safe_reply(update, "An error occurred while generating the add link.")
        elif query.data == 'demo':
            # Start simulation/demo mode as if the user ran /simulate
            try:
                await simulate_command(update, context)
            except Exception as e:
                logger.error(f"Error starting demo: {e}")
                await safe_reply(update, "Could not start demo mode.")

def add_handlers(application: Application):
    """Registers all command and callback handlers with the application."""
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("live", live_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("racecontrol", racecontrol_command))
    application.add_handler(CommandHandler("commentary", commentary_command))
    application.add_handler(CommandHandler("autocomm", autocommentary_command))
    application.add_handler(CommandHandler("standings", standings_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("simulate", simulate_command)) # For the demo

    # Callback query handler (for buttons)
    application.add_handler(CallbackQueryHandler(button_callback))

# --- Web Server for Replit Uptime ---
app = Flask('')

@app.route('/')
def home():
    return "F1 Bot is alive!"

def run_web_server():
  app.run(host='0.0.0.0', port=8080)

def start_web_server_thread():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

def main():
    """Main function to run the bot (for direct execution)."""
    from dotenv import load_dotenv
    import os

    # Start the web server in a background thread for uptime monitoring
    start_web_server_thread()

    load_dotenv()
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables or .env file.")

    application = Application.builder().token(TOKEN).build()
    add_handlers(application)
    application.run_polling()

if __name__ == '__main__':
    main()
