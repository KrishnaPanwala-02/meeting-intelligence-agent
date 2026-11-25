"""Folders: meetings grouped by their owner, and later meetings remembering the earlier ones."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta

import pytest

from src.agent.summarizer import MeetingSummarizer
from src.api import main
from src.models.database import (ActionItemDB, AskHistory, Folder, Meeting, Summary, Transcript, User)
from src.models.schemas import Decision, KeyPoint, MeetingSummary
from src.services import ask, folders, pipeline
from tests.conftest import TEST_WAV
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

BASE = datetime(2026, 11, 10, 10, 0)


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com")}
    return type("W", (), dict(h=h, client=client, Session=Session))


def mk(db, title, owner="k@example.com", day=0, folder=None, status="completed", summary=None,
       decisions=(), tasks=(), participants=()):
    mid = str(uuid.uuid4())
    when = BASE + timedelta(days=day)
    db.add(Meeting(id=mid, title=title, owner_email=owner, status=status, scheduled_time=when, created_at=when,
                   participants=list(participants), folder_id=folder))
    db.flush()
    if summary:
        db.add(Summary(id=str(uuid.uuid4()), meeting_id=mid, executive_summary=summary, key_points=[],
                       decisions=[{"decision": d, "owner": "TBD", "context": ""} for d in decisions],
                       next_steps=[], key_quotes=[], created_at=when))
    for who, task, state, due in tasks:
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task=task, assignee=who, status=state,
                            due_date=due, priority="medium"))
    db.commit()
    return mid


def uid(db, email):
    return db.query(User).filter(User.email == email).one().id


def make_folder(world, who="k", name="Zomato project"):
    r = world.client.post("/api/v1/folders", json={"name": name}, headers=world.h[who])
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ------------------------------------------------------------------ folders

def test_a_folder_can_be_made_listed_renamed_and_removed(world):
    fid = make_folder(world)
    listed = world.client.get("/api/v1/folders", headers=world.h["k"]).json()
    assert listed["count"] == 1 and listed["folders"][0]["name"] == "Zomato project" and listed["folders"][0]["meeting_count"] == 0
    r = world.client.patch(f"/api/v1/folders/{fid}", json={"name": "  Zomato   Phase 2 "}, headers=world.h["k"])
    assert r.status_code == 200 and r.json()["name"] == "Zomato Phase 2"
    assert world.client.delete(f"/api/v1/folders/{fid}", headers=world.h["k"]).json() == {"deleted": True, "meetings_unfiled": 0}
    assert world.client.get("/api/v1/folders", headers=world.h["k"]).json()["count"] == 0


def test_folder_names_are_unique_per_person_ignoring_case_and_spacing(world):
    make_folder(world, "k", "Zomato project")
    for name in ("zomato PROJECT", "  Zomato   project  "):
        assert world.client.post("/api/v1/folders", json={"name": name}, headers=world.h["k"]).status_code == 409
    make_folder(world, "r", "Zomato project")                       # someone else may use the same name
    other = make_folder(world, "k", "Swiggy")
    assert world.client.patch(f"/api/v1/folders/{other}", json={"name": "ZOMATO project"}, headers=world.h["k"]).status_code == 409


def test_folder_names_are_checked(world):
    for bad in ("", "   ", "x" * 81, "bad\x00name"):
        r = world.client.post("/api/v1/folders", json={"name": bad}, headers=world.h["k"])
        assert r.status_code == 422, bad


def test_folders_are_private_to_their_owner(world):
    fid = make_folder(world, "k")
    assert world.client.get("/api/v1/folders", headers=world.h["r"]).json()["count"] == 0
    for call in (lambda: world.client.get(f"/api/v1/folders/{fid}", headers=world.h["r"]),
                 lambda: world.client.patch(f"/api/v1/folders/{fid}", json={"name": "x"}, headers=world.h["r"]),
                 lambda: world.client.delete(f"/api/v1/folders/{fid}", headers=world.h["r"])):
        assert call().status_code == 404
    assert world.client.get("/api/v1/folders").status_code == 401


def test_deleting_a_folder_keeps_its_meetings(world):
    fid = make_folder(world)
    with world.Session() as db:
        a = mk(db, "One", folder=fid, summary="s")
        b = mk(db, "Two", day=7, folder=fid)
    r = world.client.delete(f"/api/v1/folders/{fid}", headers=world.h["k"])
    assert r.json() == {"deleted": True, "meetings_unfiled": 2}
    with world.Session() as db:
        assert db.get(Meeting, a).folder_id is None and db.get(Meeting, b).folder_id is None
        assert db.query(Summary).filter(Summary.meeting_id == a).count() == 1        # nothing else lost


# ------------------------------------------------ putting meetings in folders

def test_an_upload_can_go_in_an_existing_folder_or_make_a_new_one(world):
    fid = make_folder(world)
    data = open(TEST_WAV, "rb").read()

    def up(**form):
        return world.client.post("/api/v1/meetings/upload", headers=world.h["k"], data={"title": "Weekly", **form},
                                 files={"audio_file": ("a.wav", data)})
    r = up(folder_id=fid)
    assert r.status_code == 201 and r.json()["folder"] == {"id": fid, "name": "Zomato project"}
    r = up(new_folder="Fresh Project")
    assert r.status_code == 201 and r.json()["folder"]["name"] == "Fresh Project"
    r = up(new_folder="fresh   project")                              # same folder, not a copy
    assert r.json()["folder"]["name"] == "Fresh Project"
    assert world.client.get("/api/v1/folders", headers=world.h["k"]).json()["count"] == 2
    assert up().json()["folder"] is None


def test_an_upload_into_someone_elses_folder_is_refused_and_nothing_is_kept(world):
    theirs = make_folder(world, "r")
    r = world.client.post("/api/v1/meetings/upload", headers=world.h["k"], data={"title": "T", "folder_id": theirs},
                          files={"audio_file": ("a.wav", open(TEST_WAV, "rb").read())})
    assert r.status_code == 404
    with world.Session() as db:
        assert db.query(Meeting).count() == 0


def test_a_scheduled_series_goes_into_the_folder(world):
    from tests.test_security import future
    r = world.client.post("/api/v1/meetings/schedule", headers=world.h["k"], json={
        "title": "Standup", "scheduled_time": future(), "repeat": "weekly", "repeat_count": 3, "new_folder": "Standups"})
    assert r.status_code == 201, r.text
    fid = world.client.get("/api/v1/folders", headers=world.h["k"]).json()["folders"][0]
    assert fid["name"] == "Standups" and fid["meeting_count"] == 3


def test_a_meeting_can_be_moved_into_a_folder_and_out_again(world):
    fid = make_folder(world)
    with world.Session() as db:
        m = mk(db, "Kickoff")
    url = f"/api/v1/meetings/{m}/folder"
    r = world.client.put(url, json={"folder_id": fid}, headers=world.h["k"])
    assert r.status_code == 200 and r.json()["folder"]["name"] == "Zomato project"
    r = world.client.put(url, json={"new_folder": "Brand new"}, headers=world.h["k"])
    assert r.json()["folder"]["name"] == "Brand new"
    r = world.client.put(url, json={"folder_id": None}, headers=world.h["k"])
    assert r.status_code == 200 and r.json()["folder"] is None


def test_only_the_owner_can_file_a_meeting_and_only_in_their_own_folder(world):
    mine, theirs = make_folder(world, "k"), make_folder(world, "r")
    with world.Session() as db:
        m = mk(db, "Kickoff", participants=["r@example.com"])
        legacy = mk(db, "Old", owner=None)
    url = f"/api/v1/meetings/{m}/folder"
    assert world.client.put(url, json={"folder_id": theirs}, headers=world.h["k"]).status_code == 404     # not yours
    assert world.client.put(url, json={"folder_id": theirs}, headers=world.h["r"]).status_code == 403     # a participant can't edit
    assert world.client.put(f"/api/v1/meetings/{legacy}/folder", json={"folder_id": mine}, headers=world.h["k"]).status_code in (403, 404)
    assert world.client.put(url, json={"folder_id": mine}, headers=world.h["k"]).status_code == 200


def test_the_folder_is_shown_to_the_owner_only(world):
    fid = make_folder(world)
    with world.Session() as db:
        m = mk(db, "Kickoff", folder=fid, participants=["r@example.com"])
    mine = world.client.get(f"/api/v1/meetings/{m}", headers=world.h["k"]).json()
    theirs = world.client.get(f"/api/v1/meetings/{m}", headers=world.h["r"]).json()
    assert mine["folder"]["name"] == "Zomato project" and mine["folder"]["number"] == 1
    assert theirs["folder"] is None


def test_the_meetings_list_can_be_limited_to_a_folder(world):
    fid, other = make_folder(world, "k", "A"), make_folder(world, "k", "B")
    with world.Session() as db:
        mk(db, "In A", folder=fid)
        mk(db, "In B", day=1, folder=other)
        mk(db, "Loose", day=2)
    out = world.client.get(f"/api/v1/meetings?folder_id={fid}", headers=world.h["k"]).json()
    assert [m["title"] for m in out["meetings"]] == ["In A"]
    assert world.client.get(f"/api/v1/meetings?folder_id={fid}", headers=world.h["r"]).status_code == 404


# --------------------------------------------------------------- numbering

def test_meetings_are_numbered_by_date_not_by_when_they_were_added(world):
    fid = make_folder(world)
    with world.Session() as db:
        late = mk(db, "Third", day=14, folder=fid)
        early = mk(db, "First", day=0, folder=fid)
        mid = mk(db, "Second", day=7, folder=fid)
        assert [folders.number_of(db, db.get(Meeting, i)) for i in (early, mid, late)] == [1, 2, 3]
    out = world.client.get(f"/api/v1/folders/{fid}", headers=world.h["k"]).json()
    assert [(m["number"], m["title"]) for m in out["meetings"]] == [(1, "First"), (2, "Second"), (3, "Third")]
    assert out["meeting_count"] == 3


def test_the_timeline_shows_summaries_follow_ups_and_task_counts(world):
    fid = make_folder(world)
    with world.Session() as db:
        a = mk(db, "One", folder=fid, summary="We started.",
               tasks=[("Rahul", "Write spec", "pending", None), ("Anna", "Book room", "completed", None)])
        db.query(Summary).filter(Summary.meeting_id == a).update({"follow_up": "Continues nothing."})
        db.commit()
    first = world.client.get(f"/api/v1/folders/{fid}", headers=world.h["k"]).json()["meetings"][0]
    assert first["summary"] == "We started." and first["follow_up"] == "Continues nothing."
    assert (first["open_tasks"], first["done_tasks"]) == (1, 1)


# ------------------------------------------------------------------ memory

def test_the_first_meeting_has_nothing_to_remember(world):
    fid = make_folder(world)
    with world.Session() as db:
        a = mk(db, "One", folder=fid, summary="We started.")
        assert folders.memory_text(db, db.get(Meeting, a)) is None
        assert folders.memory_text(db, db.get(Meeting, mk(db, "Loose"))) is None          # not in a folder


def test_a_later_meeting_remembers_earlier_summaries_decisions_and_open_tasks(world):
    fid = make_folder(world)
    with world.Session() as db:
        mk(db, "Kickoff", folder=fid, summary="We chose the dashboard first.", decisions=["Launch on Friday"],
           tasks=[("Rahul", "Write the spec", "pending", datetime(2026, 11, 20)),
                  ("Anna", "Book the room", "completed", None)])
        mk(db, "Review", day=7, folder=fid, summary="Charts are late.")
        third = mk(db, "Wrap-up", day=14, folder=fid, summary="Not yet written")
        text = folders.memory_text(db, db.get(Meeting, third))
    assert 'folder "Zomato project"' in text
    assert "Meeting 1 - Kickoff (10 Nov 2026): We chose the dashboard first. Decisions: Launch on Friday." in text
    assert "Meeting 2 - Review" in text and "Charts are late." in text
    assert "Rahul: Write the spec, due 20 Nov 2026 (from Meeting 1)" in text
    assert "Book the room" not in text                       # finished tasks are not "open"
    assert "Wrap-up" not in text and "Not yet written" not in text        # a meeting does not remember itself


def test_memory_leaves_out_later_meetings_other_folders_and_cancelled_meetings(world):
    fid, other = make_folder(world, "k", "A"), make_folder(world, "k", "B")
    with world.Session() as db:
        mk(db, "Earlier", day=0, folder=fid, summary="Earlier facts.")
        mk(db, "Elsewhere", day=1, folder=other, summary="Other folder facts.")
        mk(db, "Cancelled", day=2, folder=fid, status="cancelled", summary="Cancelled facts.")
        now = mk(db, "Now", day=5, folder=fid)
        mk(db, "Future", day=9, folder=fid, summary="Future facts.")
        text = folders.memory_text(db, db.get(Meeting, now))
    assert "Earlier facts." in text
    assert not any(x in text for x in ("Other folder facts.", "Cancelled facts.", "Future facts."))


def test_memory_reads_only_the_latest_few_meetings_but_all_open_tasks(world, monkeypatch):
    monkeypatch.setattr(folders, "MEMORY_MEETINGS", 2)
    fid = make_folder(world)
    with world.Session() as db:
        for i in range(4):
            mk(db, f"M{i}", day=i, folder=fid, summary=f"Summary {i}.",
               tasks=[("Rahul", f"Task {i}", "pending", None)])
        now = mk(db, "Now", day=10, folder=fid)
        text = folders.memory_text(db, db.get(Meeting, now))
    assert "Summary 3." in text and "Summary 2." in text and "Summary 1." not in text
    assert all(f"Task {i}" in text for i in range(4))
    assert "Meeting 4 - M3" in text                           # numbers stay the folder's own


def test_a_long_memory_drops_the_least_important_parts_first(world, monkeypatch):
    fid = make_folder(world)
    with world.Session() as db:
        for i in range(4):
            mk(db, f"M{i}", day=i, folder=fid, summary="word " * 20,
               tasks=[("Rahul", f"Task {i}", "pending", datetime(2026, 12, 1 + i))])
        now = mk(db, "Now", day=10, folder=fid)
        full = folders.memory_text(db, db.get(Meeting, now))
        # a limit that fits the meetings but not the tasks: tasks go first (the latest due first)
        monkeypatch.setattr(folders, "MEMORY_MAX_CHARS", len(full) - 40)
        trimmed = folders.memory_text(db, db.get(Meeting, now))
        assert "Task 0" in trimmed and "Task 3" not in trimmed and all(f"M{i}" in trimmed for i in range(4))
        # a limit that fits one meeting: the oldest go first, the newest earlier meeting stays
        monkeypatch.setattr(folders, "MEMORY_MAX_CHARS", 230)
        tight = folders.memory_text(db, db.get(Meeting, now))
    assert len(tight) <= 230 and "M3" in tight and "M0" not in tight and "Task" not in tight


def test_the_newest_earlier_meeting_survives_even_a_tiny_limit(world, monkeypatch):
    monkeypatch.setattr(folders, "MEMORY_MAX_CHARS", 120)
    fid = make_folder(world)
    with world.Session() as db:
        mk(db, "Only", day=0, folder=fid, summary="word " * 80)
        now = mk(db, "Now", day=5, folder=fid)
        text = folders.memory_text(db, db.get(Meeting, now))
    assert len(text) <= 120 and "Meeting 1 - Only" in text and text.endswith("…")


# --------------------------------------------- the summary step uses memory

def _generated():
    return MeetingSummary(
        meeting_title="Wrap-up", executive_summary="We finished.", key_points=[KeyPoint(topic="T", description="d")],
        decisions=[Decision(decision="Ship it")], next_steps=["Celebrate"], key_quotes=[], participants=[],
        duration_minutes=1, word_count=3, created_at=datetime.now())


class Summ:
    def __init__(self, answer="Continues Meeting 1: the spec was finished.", fail=False):
        self.answer, self.fail, self.asked = answer, fail, []

    async def summarize(self, transcript, meeting_title="", participants=None):
        return _generated()

    async def follow_up(self, memory, title, number, summary, decisions=None, next_steps=None):
        self.asked.append(dict(memory=memory, title=title, number=number, summary=summary,
                               decisions=decisions, next_steps=next_steps))
        if self.fail:
            raise RuntimeError("model fell over")
        return self.answer


def _with_transcript(db, mid):
    db.add(Transcript(id=str(uuid.uuid4()), meeting_id=mid, language="en", full_text="hello", word_count=1,
                      speaker_count=1, created_at=datetime.utcnow(),
                      segments=[{"id": 0, "start_time": 0, "end_time": 3, "speaker": "Speaker 1", "text": "hello"}]))
    db.commit()


def test_a_meeting_in_a_folder_gets_a_follow_up_from_what_came_before(world):
    fid = make_folder(world)
    with world.Session() as db:
        mk(db, "Kickoff", folder=fid, summary="We chose the dashboard.")
        now = mk(db, "Wrap-up", day=7, folder=fid)
        _with_transcript(db, now)
        s = Summ()
        row, _ = asyncio.run(pipeline.summarize_step(db, db.get(Meeting, now), summarizer=s))
        assert row.follow_up == "Continues Meeting 1: the spec was finished."
        assert row.executive_summary == "We finished."                  # the summary itself is untouched
    asked = s.asked[0]
    assert asked["number"] == 2 and "We chose the dashboard." in asked["memory"]
    assert asked["decisions"] == ["Ship it"] and asked["next_steps"] == ["Celebrate"]


def test_the_first_meeting_and_unfiled_meetings_get_no_follow_up_and_no_extra_model_call(world):
    fid = make_folder(world)
    with world.Session() as db:
        first = mk(db, "Kickoff", folder=fid)
        loose = mk(db, "Loose", day=1)
        for mid in (first, loose):
            _with_transcript(db, mid)
            s = Summ()
            row, _ = asyncio.run(pipeline.summarize_step(db, db.get(Meeting, mid), summarizer=s))
            assert row.follow_up is None and s.asked == []


def test_a_failed_follow_up_does_not_lose_the_summary(world):
    fid = make_folder(world)
    with world.Session() as db:
        mk(db, "Kickoff", folder=fid, summary="We chose the dashboard.")
        now = mk(db, "Wrap-up", day=7, folder=fid)
        _with_transcript(db, now)
        row, _ = asyncio.run(pipeline.summarize_step(db, db.get(Meeting, now), summarizer=Summ(fail=True)))
        assert row.follow_up is None and row.executive_summary == "We finished."


def test_regenerating_a_summary_uses_only_the_meetings_before_it(world):
    fid = make_folder(world)
    with world.Session() as db:
        first = mk(db, "Kickoff", day=0, folder=fid, summary="Kickoff facts.")
        _with_transcript(db, first)
        mk(db, "Later", day=9, folder=fid, summary="Later facts.")
        s = Summ()
        asyncio.run(pipeline.summarize_step(db, db.get(Meeting, first), regenerate=True, summarizer=s))
    assert s.asked == []                                      # nothing before the first meeting, whatever came after


def test_the_summary_endpoint_returns_the_follow_up(world):
    fid = make_folder(world)
    with world.Session() as db:
        a = mk(db, "One", folder=fid, summary="We started.")
        db.query(Summary).filter(Summary.meeting_id == a).update({"follow_up": "Builds on the kickoff."})
        db.commit()
    assert world.client.get(f"/api/v1/meetings/{a}/summary", headers=world.h["k"]).json()["follow_up"] == "Builds on the kickoff."


# ------------------------------------------------ the model's follow-up prompt

@pytest.fixture
def summarizer(monkeypatch):
    s = MeetingSummarizer(ollama_host="http://x")
    s.prompts, s.reply = [], "Continues Meeting 1."

    async def fake(prompt, schema=None, num_predict=800):
        s.prompts.append(prompt)
        return s.reply
    monkeypatch.setattr(s, "_call_ollama", fake)
    return s


def test_the_follow_up_prompt_holds_the_memory_and_the_new_meeting_and_forbids_guessing(summarizer):
    summarizer.reply = "Follows up: Rahul finished the spec (Meeting 1).\nStill open: Anna: book the room"
    out = asyncio.run(summarizer.follow_up("Meeting 1 - Kickoff: chose dashboard.", "Review", 2, "Charts are late.",
                                           ["Delay charts"], ["Fix charts"]))
    assert out == "Follows up: Rahul finished the spec (Meeting 1).\nStill open: Anna: book the room"
    p = summarizer.prompts[0]
    assert "Meeting 1 - Kickoff: chose dashboard." in p and "NEWEST MEETING (Meeting 2, Review)" in p
    assert "Charts are late." in p and "Decisions: Delay charts" in p and "Next steps: Fix charts" in p
    assert "Never guess" in p and "Do not combine" in p


def test_the_models_three_lines_are_cleaned_up(summarizer):
    summarizer.reply = ("Here you go:\n\n**Follows up:** The data problem was fixed (Meeting 2).\n"
                        "New: Charts will be added next week.\n- Still open: - Anna: book the review room\n"
                        "Follows up: a second line is ignored")
    out = asyncio.run(summarizer.follow_up("m", "T", 3, "s"))
    assert out == ("Follows up: The data problem was fixed (Meeting 2).\n"
                   "New: Charts will be added next week.\n"
                   "Still open: Anna: book the review room")


@pytest.mark.parametrize("reply", [
    "", "No connection found.", "Follows up: nothing\nNew: a new thing\nStill open: nothing",
    "New: only something new", "Follows up: None\nStill open: n/a", "just a sentence with no labels",
])
def test_no_link_found_means_no_follow_up(summarizer, reply):
    summarizer.reply = reply
    assert asyncio.run(summarizer.follow_up("m", "T", 2, "s")) is None


def test_a_line_with_only_open_tasks_still_counts_and_nothing_lines_are_dropped(summarizer):
    summarizer.reply = "Follows up: nothing\nNew: x\nStill open: Anna: book the room"
    assert asyncio.run(summarizer.follow_up("m", "T", 2, "s")) == "New: x\nStill open: Anna: book the room"


def test_a_line_that_names_the_newest_meeting_as_the_earlier_one_is_dropped(summarizer):
    summarizer.reply = ("Follows up: The decision was made in Meeting 2, which was not in Meeting 1.\n"
                        "New: Rahul will fix the data (Meeting 2 told us).\n"
                        "Still open: Rahul: spec (from Meeting 1)")
    out = asyncio.run(summarizer.follow_up("m", "T", 2, "s"))
    assert out == "New: Rahul will fix the data (Meeting 2 told us).\nStill open: Rahul: spec (from Meeting 1)"
    summarizer.reply = "Follows up: The decision was made in Meeting 2."          # only a slip: nothing left
    assert asyncio.run(summarizer.follow_up("m", "T", 2, "s")) is None


def test_a_very_long_line_is_cut(summarizer):
    summarizer.reply = "Follows up: " + "x" * 2000
    out = asyncio.run(summarizer.follow_up("m", "T", 2, "s"))
    assert len(out) == len("Follows up: ") + MeetingSummarizer.FOLLOW_LINE_CHARS


# ------------------------------------------------------------ ask a folder

def test_asking_a_folder_searches_only_its_meetings(world):
    fid = make_folder(world)
    with world.Session() as db:
        a = mk(db, "Budget talk", day=0, folder=fid, summary="Budget is approved.")
        mk(db, "Budget elsewhere", day=1, summary="Budget is rejected.")
        user = db.query(User).filter(User.email == "k@example.com").one()
        ctx, tags, searched = ask.build_context(db, user, "budget", None, within=folders.ordered(db, fid))
    assert searched == 1 and "Budget is approved." in ctx and "rejected" not in ctx


def test_the_ask_endpoint_can_be_limited_to_a_folder_and_keeps_its_own_conversation(world, monkeypatch):
    async def fake_answer(prompt):
        return "Approved [M1]."
    monkeypatch.setattr(main.qa, "answer", fake_answer)
    fid = make_folder(world)
    with world.Session() as db:
        mk(db, "Budget talk", folder=fid, summary="Budget is approved.")
    r = world.client.post("/api/v1/ask", headers=world.h["k"], json={"question": "Is the budget approved?", "folder_id": fid})
    assert r.status_code == 200 and r.json()["searched"] == 1
    # saved under the folder, not under "all meetings"
    assert world.client.get(f"/api/v1/ask/history?folder_id={fid}", headers=world.h["k"]).json()["turns"][0]["question"] == "Is the budget approved?"
    assert world.client.get("/api/v1/ask/history", headers=world.h["k"]).json()["turns"] == []
    assert world.client.delete(f"/api/v1/ask/history?folder_id={fid}", headers=world.h["k"]).status_code == 204
    assert world.client.get(f"/api/v1/ask/history?folder_id={fid}", headers=world.h["k"]).json()["turns"] == []


def test_ask_refuses_both_scopes_and_other_peoples_folders(world):
    mine, theirs = make_folder(world, "k"), make_folder(world, "r")
    with world.Session() as db:
        m = mk(db, "One", folder=mine, summary="s")
    q = {"question": "What happened?"}
    assert world.client.post("/api/v1/ask", headers=world.h["k"], json={**q, "folder_id": mine, "meeting_id": m}).status_code == 422
    assert world.client.post("/api/v1/ask", headers=world.h["k"], json={**q, "folder_id": theirs}).status_code == 404
    assert world.client.get(f"/api/v1/ask/history?folder_id={theirs}", headers=world.h["k"]).status_code == 404


# ------------------------------------------------------------------ renaming a meeting

def test_a_meeting_can_be_renamed_whatever_its_status(world):
    with world.Session() as db:
        done = mk(db, "old name", status="completed", summary="Kept")
    r = world.client.patch(f"/api/v1/meetings/{done}/title", json={"title": "  New   name  "}, headers=world.h["k"])
    assert r.status_code == 200 and r.json()["title"] == "New name"
    with world.Session() as db:
        assert db.get(Meeting, done).title == "New name"
        assert db.query(Summary).filter(Summary.meeting_id == done).one().executive_summary == "Kept"   # nothing else changed


def test_renaming_is_checked_and_limited_to_people_who_may_edit(world):
    with world.Session() as db:
        mid = mk(db, "keep", participants=["r@example.com"])
    url = f"/api/v1/meetings/{mid}/title"
    assert world.client.patch(url, json={"title": "   "}, headers=world.h["k"]).status_code == 422
    assert world.client.patch(url, json={"title": "x" * 201}, headers=world.h["k"]).status_code == 422
    assert world.client.patch(url, json={"title": "Hacked"}, headers=world.h["r"]).status_code == 403   # a participant may only read
    assert world.client.patch(url, json={"title": "Hacked"}).status_code == 401
    with world.Session() as db:
        assert db.get(Meeting, mid).title == "keep"
