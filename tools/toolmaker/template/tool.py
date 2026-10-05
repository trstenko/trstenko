"""A working example to change into your own tool.

How a tool is called
    The model decides to use it from the description in tool.json, and sends
    the arguments declared there. The device checks them (type, length,
    range) and then calls your function in a process of its own:

        def get_weather(ctx, city=None): ...

    An optional argument the model left out is not passed at all, so give it
    a default.

What `ctx` gives you
    ctx.http.get(url, params=None, headers=None)
    ctx.http.post(url, json=None, headers=None)
        HTTPS only, and only to the hosts under permissions.internet in
        tool.json. Returns a response with .status, .ok, .text and .json().
        At most 4 requests in one call, 5 seconds each, 64 KB of answer.
    ctx.storage.get(key, default=None) / .set(key, value) / .delete(key)
        8 KB of JSON that belongs to this tool. Needs "storage": true.
    ctx.lang    "en" or "sl": the language the owner speaks
    ctx.now     the local time, "2026-10-02T15:10"

What to return
    A small dict of facts. The model reads it and words the answer itself, in
    the owner's language. Keep it short: every character is paid for on the
    next request, and anything over 1000 characters is cut.

When it cannot work
    raise ToolError("...") with a sentence a person could hear. The model
    will say something close to it.

What a tool may not do
    Open sockets, start programs, write files or read anything outside its
    own folder. The whole call has 8 seconds.
"""

from trsttool import ToolError

# WMO weather codes, grouped into words the model can use.
_SKY = (
    ((0,), "clear"),
    ((1, 2), "partly cloudy"),
    ((3,), "overcast"),
    ((45, 48), "foggy"),
    ((51, 53, 55, 56, 57), "drizzle"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "rain"),
    ((71, 73, 75, 77, 85, 86), "snow"),
    ((95, 96, 99), "thunderstorm"),
)


def _sky(code):
    for codes, words in _SKY:
        if code in codes:
            return words
    return "unknown"


def get_weather(ctx, city=None):
    city = (city or ctx.storage.get("city") or "").strip()
    if not city:
        raise ToolError("Which city do you mean?")

    found = ctx.http.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1,
                                 "language": ctx.lang})
    if not found.ok:
        raise ToolError("The weather service is not answering right now.")
    places = found.json().get("results") or []
    if not places:
        raise ToolError("I could not find a place called %s." % city)
    place = places[0]

    forecast = ctx.http.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": place["latitude"], "longitude": place["longitude"],
        "current": "temperature_2m,weather_code,wind_speed_10m"})
    if not forecast.ok:
        raise ToolError("The weather service is not answering right now.")
    current = forecast.json().get("current")
    if not current:
        raise ToolError("The weather service sent nothing I can read.")

    ctx.storage.set("city", place["name"])
    return {
        "place": place["name"],
        "country": place.get("country", ""),
        "degrees_c": round(current["temperature_2m"]),
        "sky": _sky(current.get("weather_code")),
        "wind_kmh": round(current.get("wind_speed_10m", 0)),
    }
