"""Tests for this tool. Run from the tool creator; never installed on a device.

Every function called test_... is run and given `run`:

    r = run("get_weather", city="Maribor", internet={...})

    internet   the answers the internet gives, by address without the query:
               {"https://host/path": {"status": 200, "json": {...}}}
               ("text" in place of "json" for an answer that is not JSON).
               An address that is not listed answers 404.
    storage    what the tool's storage holds before the call
    down=True  no internet at all
    lang, now  what ctx.lang and ctx.now will be

and gets back:

    r.ok        did the tool return
    r.data      what it returned
    r.error     what the model is told if it did not
    r.reads     the exact text the model reads
    r.requests  the requests it made
    r.storage   its storage afterwards
"""

SEARCH = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"

MARIBOR = {"results": [{"name": "Maribor", "country": "Slovenia",
                        "latitude": 46.55, "longitude": 15.65}]}
SUNNY = {"current": {"temperature_2m": 17.6, "weather_code": 0,
                     "wind_speed_10m": 7.2}}
INTERNET = {SEARCH: {"json": MARIBOR}, FORECAST: {"json": SUNNY}}


def test_it_says_the_weather(run):
    r = run("get_weather", city="Maribor", internet=INTERNET)
    assert r.ok, r.error
    assert r.data == {"place": "Maribor", "country": "Slovenia",
                      "degrees_c": 18, "sky": "clear", "wind_kmh": 7}


def test_it_asks_the_service_for_the_right_place(run):
    r = run("get_weather", city="Maribor", internet=INTERNET)
    assert "name=Maribor" in r.requests[0]["url"]
    assert "latitude=46.55" in r.requests[1]["url"]


def test_it_remembers_the_last_city(run):
    r = run("get_weather", city="Maribor", internet=INTERNET)
    assert r.storage == {"city": "Maribor"}
    again = run("get_weather", internet=INTERNET, storage=r.storage)
    assert again.ok and again.data["place"] == "Maribor"


def test_with_no_city_at_all_it_asks(run):
    r = run("get_weather")
    assert not r.ok and "Which city" in r.error
    assert r.requests == []


def test_a_place_that_does_not_exist(run):
    r = run("get_weather", city="Qwxzy",
            internet={SEARCH: {"json": {}}})
    assert not r.ok and "could not find" in r.error


def test_the_service_is_down(run):
    r = run("get_weather", city="Maribor",
            internet={SEARCH: {"status": 503, "text": "busy"}})
    assert not r.ok and "not answering" in r.error


def test_the_answer_is_short_enough_to_send_to_the_model(run):
    r = run("get_weather", city="Maribor", internet=INTERNET)
    assert len(r.reads) < 400, len(r.reads)
