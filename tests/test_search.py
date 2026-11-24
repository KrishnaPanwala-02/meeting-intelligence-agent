"""Global search: matching rules, snippets, ranking, and above all who can be searched."""

import uuid
from datetime import datetime, timedelta

import pytest

from src.models.database import ActionItemDB, Meeting, Summary, Transcript, User
from src.services import search as sv
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401


# ----------------------------------------------------------------- parsing

def test_parse_query():
    assert sv.parse_query("Rahul  Budget") == ["rahul", "budget"]
    assert sv.parse_query('"project report" Rahul') == ["project report", "rahul"]
    assert sv.parse_query("a Rahul rahul, 'x'") == ["rahul"]                     # short words and repeats dropped
    assert sv.parse_query('say "unfinished phrase') == ["say", "unfinished", "phrase"]
    assert sv.parse_query('"" Rahul') == ["rahul"]
    assert sv.parse_query("   ") == [] and sv.parse_query("") == [] and sv.parse_query(None) == []
    assert len(sv.parse_query("word " * 300)) == 1


def test_matches_needs_every_term():
    assert sv.matches("Rahul writes the Project Report", ["rahul", "project report"])
    assert not sv.matches("Rahul writes the plan", ["rahul", "report"])
    assert not sv.matches("anything", []) and not sv.matches(None, ["x"])


def test_snippet_centres_on_the_match_and_marks_the_cut():
    text = "word " * 40 + "NEEDLE here " + "tail " * 60
    s = sv.snippet(text, ["needle"])
    assert "NEEDLE" in s and s.startswith("…") and s.endswith("…") and len(s) < 200
    assert sv.snippet("short NEEDLE text", ["needle"]) == "short NEEDLE text"
    assert sv.snippet("no hit anywhere", ["zzz"]) == "no hit anywhere"


# --------------------------------------------------------------- fixtures

def seg(i, speaker, text):
    return {"id": i, "start_time": i * 10.0, "end_time": i * 10 + 8, "speaker": speaker, "text": text}


def add(db, title, owner, days_ago=0, participants=(), segments=None, summary=None, tasks=()):
    mid = str(uuid.uuid4())
    when = datetime(2026, 11, 13, 10, 0) - timedelta(days=days_ago)
    db.add(Meeting(id=mid, title=title, owner_email=owner, status="completed", participants=list(participants),
                   created_at=when, scheduled_time=when))
    if segments is not None:
        db.add(Transcript(id=str(uuid.uuid4()), meeting_id=mid, language="en", full_text="x", segments=segments,
                          word_count=5, speaker_count=1, created_at=when))
    if summary:
        db.add(Summary(id=str(uuid.uuid4()), meeting_id=mid, created_at=when, executive_summary=summary,
                       key_points=[{"topic": "Reports", "description": "The quarterly report is late"}],
                       decisions=[{"decision": "Ship the report on Friday", "owner": "Rahul Patel", "context": ""}],
                       next_steps=["Rahul sends the report"], key_quotes=[]))
    for task, who, status in tasks:
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task=task, assignee=who, status=status,
                            priority="medium", due_date=datetime(2026, 11, 16)))
    db.commit()
    return mid


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"), "x": signup(client, "x@example.com")}
    ids = {}
    with Session() as db:
        ids["zomato"] = add(db, "ZOMATO PROJECT", "k@example.com", participants=["r@example.com"],
                            segments=[seg(0, "Krishna Panwala", "I will finish the model training."),
                                      seg(1, "Rahul Patel", "I will prepare the project report by Monday."),
                                      seg(2, "Rahul Patel", "The report needs the budget numbers.")],
                            summary="Rahul writes the project report; Krishna trains the model.",
                            tasks=[("Prepare the project report", "Rahul Patel", "pending")])
        ids["budget"] = add(db, "Budget review", "k@example.com", days_ago=30,
                            segments=[seg(0, "Krishna Panwala", "The budget was cut by ten percent.")])
        ids["secret"] = add(db, "Private plans", "x@example.com", days_ago=1,
                            segments=[seg(0, "Mallory", "TOPSECRET report about the budget and Rahul.")],
                            summary="TOPSECRET report.", tasks=[("Guard the TOPSECRET report", "Rahul Patel", "pending")])
    return type("W", (), dict(h=h, ids=ids, client=client, Session=Session))


def find(w, who, q):
    r = w.client.get("/api/v1/search", params={"q": q}, headers=w.h[who])
    assert r.status_code == 200, r.text
    return r.json()


def kinds(result, meeting_id):
    m = next(m for m in result["meetings"] if m["meeting_id"] == meeting_id)
    return [h["kind"] for h in m["hits"]]


# ----------------------------------------------------------------- results

def test_finds_titles_summaries_tasks_and_transcript_lines(world):
    r = find(world, "k", "project report")
    assert [m["title"] for m in r["meetings"]] == ["ZOMATO PROJECT"]
    assert "title" not in kinds(r, world.ids["zomato"])      # the title is "ZOMATO PROJECT"; "report" is not in it
    m = r["meetings"][0]
    by_kind = {}
    for h in m["hits"]:
        by_kind.setdefault(h["kind"], h)
    assert {"summary", "task", "transcript"} <= set(by_kind)
    assert by_kind["task"]["assignee"] == "Rahul Patel" and by_kind["task"]["status"] == "pending" and by_kind["task"]["due"] == "2026-11-16"
    t = by_kind["transcript"]
    assert t["speaker"] == "Rahul Patel" and t["segment_id"] == 1 and t["time"] == 10.0 and "project report" in t["text"]
    assert r["terms"] == ["project", "report"] and r["total"] == m["hit_count"] and m["date"] == "2026-11-13"


def test_every_word_must_be_in_the_same_piece_of_text(world):
    assert find(world, "k", "model monday")["total"] == 0     # in different lines, so no single match
    both = find(world, "k", "model report")                   # but the summary sentence has both words
    assert [h["kind"] for h in both["meetings"][0]["hits"]] == ["summary"]
    assert find(world, "k", "model training")["meetings"][0]["title"] == "ZOMATO PROJECT"


def test_phrases_and_case_and_speaker_names(world):
    assert find(world, "k", '"project report"')["total"] > 0
    assert find(world, "k", '"report project"')["total"] == 0
    assert find(world, "k", "PROJECT REPORT")["total"] == find(world, "k", "project report")["total"]
    spoken = [h for h in find(world, "k", "Krishna model")["meetings"][0]["hits"] if h["kind"] == "transcript"]
    assert [h["speaker"] for h in spoken] == ["Krishna Panwala"] and "model training" in spoken[0]["text"]


def test_best_meeting_first_and_titles_outrank_transcript_lines(world):
    r = find(world, "k", "budget")
    assert [m["title"] for m in r["meetings"]] == ["Budget review", "ZOMATO PROJECT"]      # a title hit beats one line
    assert kinds(r, world.ids["budget"])[0] == "title"


def test_nothing_found_and_too_short_queries(world):
    assert find(world, "k", "xyzzy") == {"query": "xyzzy", "terms": ["xyzzy"], "total": 0, "meetings": []}
    assert world.client.get("/api/v1/search", params={"q": "a"}, headers=world.h["k"]).status_code == 422
    assert world.client.get("/api/v1/search", params={"q": "x" * 201}, headers=world.h["k"]).status_code == 422
    assert world.client.get("/api/v1/search", headers=world.h["k"]).status_code == 422
    assert find(world, "k", "?! .")["meetings"] == []


# ------------------------------------------------------------------- access

def test_other_peoples_meetings_are_never_found(world):
    for who in ("k", "r"):
        for q in ("TOPSECRET", "Mallory", "Private plans", "Guard", "vault report budget"):
            r = find(world, who, q)
            assert world.ids["secret"] not in [m["meeting_id"] for m in r["meetings"]], (who, q)
            assert "TOPSECRET" not in str(r["meetings"]) and "Mallory" not in str(r["meetings"])
    assert [m["title"] for m in find(world, "x", "TOPSECRET")["meetings"]] == ["Private plans"]
    assert find(world, "x", "ZOMATO")["meetings"] == []


def test_a_participant_finds_only_what_was_shared_with_them(world):
    r = find(world, "r", "budget")
    assert [m["title"] for m in r["meetings"]] == ["ZOMATO PROJECT"]                    # not "Budget review"
    assert find(world, "r", "Budget review")["meetings"] == []


def test_search_needs_signing_in(world):
    assert world.client.get("/api/v1/search", params={"q": "report"}).status_code == 401


def test_results_are_capped(world):
    with world.Session() as db:
        for i in range(40):
            add(db, f"Sprint {i}", "k@example.com", days_ago=i + 2, segments=[seg(j, "Krishna Panwala", f"sprint note {j}") for j in range(20)])
    r = find(world, "k", "sprint")
    assert len(r["meetings"]) == sv.MAX_MEETINGS
    assert all(len(m["hits"]) <= sv.HITS_PER_MEETING and m["hit_count"] >= len(m["hits"]) for m in r["meetings"])
    assert r["total"] > sum(len(m["hits"]) for m in r["meetings"])


def test_hostile_text_is_returned_as_plain_text(world):
    with world.Session() as db:
        add(db, "Hostile", "k@example.com", segments=[seg(0, "Eve", "<script>alert(1)</script> report \x00 [M7]")])
    hit = next(m for m in find(world, "k", "script report")["meetings"] if m["title"] == "Hostile")["hits"][0]
    assert "\x00" not in hit["text"] and "[M7]" not in hit["text"]
