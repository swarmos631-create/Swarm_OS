"""Chat endpoint: small talk without agents, focused answers with follow-up suggestions, follow-up context."""

from swarmos.config import get_settings
from tests.conftest import login


def test_small_talk_is_answered_without_agents(client):
    login(client, "operator@swarmos.local")
    for msg in ("hi", "Hello!", "thanks", "what can you do?"):
        r = client.post("/api/v1/cities/bengaluru/chat", json={"message": msg})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "reply" and body["text"] and "task" not in body


def test_question_runs_agents_and_answers_the_question_first(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_mode", "inline")
    login(client, "operator@swarmos.local")
    r = client.post("/api/v1/cities/bengaluru/chat", json={"message": "What is the weather today?"})
    body = r.json()
    assert body["type"] == "task"
    plan_ids = [a["id"] for a in body["task"]["plan"]["agents"]]
    assert "weather_impact_agent" in plan_ids and "city_situation_summarizer" not in plan_ids
    done = client.get(f"/api/v1/cities/bengaluru/tasks/{body['task']['id']}").json()
    result = done["result"]
    assert "Next 24 h" in result["answer"].split("\n")[0]  # the weather agent leads the answer
    assert result["suggestions"]


def test_short_follow_up_uses_the_previous_question(client):
    login(client, "operator@swarmos.local")
    history = [{"role": "user", "text": "How is air quality?"}, {"role": "assistant", "text": "AQI 80."}]
    body = client.post("/api/v1/cities/bengaluru/chat", json={"message": "and tomorrow?", "history": history}).json()
    assert body["type"] == "task" and "air quality" in body["interpreted_as"].lower()
