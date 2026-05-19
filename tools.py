"""Tool Use / Function Calling for tansaibot (#36).

Defines a registry of tools the AI can invoke, inspired by OpenAI's
function-calling API.

Built-in tools:
  - calculator     — safe arithmetic expression evaluator
  - datetime_now   — current date and time in WIB
  - web_search     — DuckDuckGo query
  - weather        — Open-Meteo free weather API (no key needed)
  - unit_convert   — common unit conversions

Usage:
    from tools import ToolRegistry
    registry = ToolRegistry()
    
    # Build a tools-aware prompt
    tools_spec = registry.get_spec_text()
    
    # Parse + execute tool calls from AI response
    result = await registry.execute_from_response(ai_response)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

logger = logging.getLogger(__name__)

_TZ_OFFSET = int(os.getenv("TZ_OFFSET_HOURS", "7"))


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        params: dict[str, str],
        fn: Callable[..., Any],
    ) -> None:
        self.name = name
        self.description = description
        self.params = params  # {param_name: description}
        self._fn = fn

    async def call(self, **kwargs: Any) -> str:
        try:
            if asyncio.iscoroutinefunction(self._fn):
                return str(await self._fn(**kwargs))
            return str(self._fn(**kwargs))
        except Exception as exc:
            return f"[Tool error: {exc}]"


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------

def _safe_calc(expr: str) -> str:
    """Safe arithmetic eval — no builtins allowed."""
    allowed = re.compile(r"^[\d\s\+\-\*\/\.\(\)\^%]+$")
    expr = expr.strip().replace("^", "**")
    if not allowed.match(expr):
        return "Error: ekspresi tidak valid"
    try:
        result = eval(expr, {"__builtins__": {}}, {"sqrt": math.sqrt, "pi": math.pi, "e": math.e})
        return str(round(result, 10))
    except Exception as exc:
        return f"Error: {exc}"


def _datetime_now() -> str:
    local = datetime.now(timezone(timedelta(hours=_TZ_OFFSET)))
    return local.strftime("%A, %d %B %Y %H:%M:%S WIB")


async def _web_search_tool(query: str) -> str:
    from web_search import search, format_results_for_prompt
    results = await search(query, max_results=3)
    return format_results_for_prompt(results, query)


async def _weather_tool(location: str) -> str:
    """Open-Meteo free weather API — no API key needed."""
    import httpx
    try:
        # First get coordinates via Nominatim
        async with httpx.AsyncClient(timeout=10) as client:
            geo = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location, "format": "json", "limit": 1},
                headers={"User-Agent": "tansaibot/2.0"},
            )
            geo_data = geo.json()
            if not geo_data:
                return f"Lokasi '{location}' tidak ditemukan."
            lat = float(geo_data[0]["lat"])
            lon = float(geo_data[0]["lon"])

            # Get weather
            w = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                    "timezone": "Asia/Jakarta",
                },
            )
            wd = w.json()
            curr = wd.get("current", {})
            temp = curr.get("temperature_2m", "?")
            humidity = curr.get("relative_humidity_2m", "?")
            wind = curr.get("wind_speed_10m", "?")
            wcode = curr.get("weather_code", 0)

            # WMO code interpretation
            desc_map = {0:"Cerah", 1:"Hampir cerah", 2:"Berawan sebagian", 3:"Mendung",
                        45:"Berkabut", 51:"Gerimis", 61:"Hujan ringan", 63:"Hujan sedang",
                        71:"Salju ringan", 80:"Hujan lebat", 95:"Badai petir"}
            desc = desc_map.get(int(wcode), f"Kode cuaca {wcode}")

            return (
                f"Cuaca di {location}:\n"
                f"🌡️ Suhu: {temp}°C | 💧 Kelembaban: {humidity}%\n"
                f"💨 Angin: {wind} km/h | ☁️ {desc}"
            )
    except Exception as exc:
        return f"Gagal mengambil data cuaca: {exc}"


def _unit_convert(value: str, from_unit: str, to_unit: str) -> str:
    """Convert common units."""
    try:
        val = float(value)
    except ValueError:
        return "Nilai tidak valid"

    conversions: dict[tuple[str, str], float] = {
        ("km", "mi"): 0.621371, ("mi", "km"): 1.60934,
        ("kg", "lb"): 2.20462, ("lb", "kg"): 0.453592,
        ("m", "ft"): 3.28084, ("ft", "m"): 0.3048,
        ("c", "f"): None, ("f", "c"): None,  # Special case
        ("l", "gal"): 0.264172, ("gal", "l"): 3.78541,
        ("usd", "idr"): 16000, ("idr", "usd"): 1/16000,
    }
    key = (from_unit.lower(), to_unit.lower())
    if key == ("c", "f"):
        result = val * 9/5 + 32
    elif key == ("f", "c"):
        result = (val - 32) * 5/9
    elif key in conversions:
        factor = conversions[key]
        if factor is None:
            return "Konversi tidak tersedia"
        result = val * factor
    else:
        return f"Konversi {from_unit} → {to_unit} tidak dikenal"

    return f"{val} {from_unit} = {round(result, 4)} {to_unit}"


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        self.register(Tool(
            "calculator",
            "Evaluasi ekspresi matematika. Gunakan untuk perhitungan numerik.",
            {"expression": "Ekspresi matematika, contoh: (2 + 3) * 4 / 2"},
            _safe_calc,
        ))
        self.register(Tool(
            "datetime_now",
            "Dapatkan tanggal dan waktu sekarang (WIB). Tidak perlu parameter.",
            {},
            _datetime_now,
        ))
        self.register(Tool(
            "web_search",
            "Cari informasi di internet menggunakan DuckDuckGo.",
            {"query": "Kata kunci pencarian"},
            _web_search_tool,
        ))
        self.register(Tool(
            "weather",
            "Dapatkan informasi cuaca terkini di suatu lokasi.",
            {"location": "Nama kota atau lokasi, contoh: Jakarta"},
            _weather_tool,
        ))
        self.register(Tool(
            "unit_convert",
            "Konversi satuan: km/mi, kg/lb, m/ft, C/F, l/gal, USD/IDR",
            {
                "value": "Nilai yang akan dikonversi",
                "from_unit": "Satuan asal (km, mi, kg, lb, c, f, usd, idr, dll)",
                "to_unit": "Satuan tujuan",
            },
            _unit_convert,
        ))

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get_spec_text(self) -> str:
        """Return a text description of all tools for injection into AI prompt."""
        lines = ["[Alat yang tersedia — gunakan format: TOOL: <nama>(<param>=<nilai>)]"]
        for t in self._tools.values():
            params_desc = ", ".join(f"{k}: {v}" for k, v in t.params.items())
            lines.append(f"- {t.name}({params_desc}): {t.description}")
        lines.append("\nSetelah memanggil alat, sertakan hasilnya dalam jawaban.")
        return "\n".join(lines)

    async def execute_from_response(self, text: str) -> str:
        """Parse and execute tool calls in AI response text.
        
        Format: TOOL: tool_name(param1=value1, param2=value2)
        Returns annotated text with tool results injected.
        """
        pattern = re.compile(r"TOOL:\s*(\w+)\(([^)]*)\)")
        results: list[tuple[str, str]] = []

        for match in pattern.finditer(text):
            tool_name = match.group(1)
            args_str = match.group(2)
            tool = self._tools.get(tool_name)
            if tool is None:
                results.append((match.group(0), f"[Tool '{tool_name}' tidak ditemukan]"))
                continue

            # Parse kwargs
            kwargs: dict[str, str] = {}
            for part in args_str.split(","):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    kwargs[k.strip()] = v.strip().strip("\"'")

            result = await tool.call(**kwargs)
            results.append((match.group(0), f"[Hasil {tool_name}: {result}]"))

        for original, replacement in results:
            text = text.replace(original, replacement, 1)

        return text


# Global default registry
_default_registry: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry
