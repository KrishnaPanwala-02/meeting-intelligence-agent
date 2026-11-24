"""Name suggestions from what a speaker says, and hand-corrections of the transcript."""

import uuid
from datetime import datetime

import pytest

from src.models.database import ActionItemDB, Meeting, Summary, Transcript, User
from src.services import transcript_edit as te
from src.services import voices
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

SENTENCE = "My name is Megapanvala and I will do the validation and testing part."


# ------------------------------------------------------------ suggest_name

def test_the_name_said_in_the_recording_is_suggested():
    s = voices.suggest_name([SENTENCE])
    assert s["heard"] == "Megapanvala" and s["name"] == "Megapanvala" and s["known"] is False
    assert s["quote"].startswith("My name is Megapanvala")


def test_a_near_spelling_of_a_known_person_is_corrected():
    s = voices.suggest_name([SENTENCE], ["Krishna Panwala", "Megha Panwala"])
    assert s["heard"] == "Megapanvala" and s["name"] == "Megha Panwala" and s["known"] is True


def test_a_first_name_alone_matches_the_full_known_name():
    s = voices.suggest_name(["Hi, I'm Rahul and I will book the room."], ["Rahul Patel", "Anna Larsen"])
    assert s["name"] == "Rahul Patel" and s["heard"] == "Rahul"


def test_unrelated_known_names_are_not_forced_on_the_speaker():
    s = voices.suggest_name(["My name is Zed."], ["Krishna Panwala", "Rahul Patel"])
    assert s["name"] == "Zed" and s["known"] is False


@pytest.mark.parametrize("text,expected", [
    ("My name is Megha Panwala.", "Megha Panwala"),
    ("my name is Anna and I lead design", "Anna"),
    ("Hello everyone, this is Priya speaking.", "Priya"),
    ("I am Rahul Patel from the data team", "Rahul Patel"),
    ("Call me Sam", "Sam"),
    ("My name is Rahul Patel and I'm from Mumbai", "Rahul Patel"),
    ("My name’s Ravi.", "Ravi"),
])
def test_ways_people_introduce_themselves(text, expected):
    assert voices.suggest_name([text])["heard"] == expected


@pytest.mark.parametrize("text", [
    "I'm Fine with that.", "I am Going to finish the report.", "I'm working on the report.",
    "This is the plan for Friday.", "I am sorry I am late.", "We will finish the model training by Friday.",
    "I'm Happy to help", "", "My name is", "I am Responsible for testing",
])
def test_sentences_that_are_not_introductions_give_nothing(text):
    assert voices.suggest_name([text]) is None


def test_an_explicit_introduction_beats_a_casual_one():
    s = voices.suggest_name(["I'm Ravi and I agree.", "My name is Rahul."])
    assert s["heard"] == "Rahul"


# -------------------------------------------------------------- fixtures

def seed(Session, owner="k@example.com"):
    mid = str(uuid.uuid4())
    with Session() as db:
        db.add(Meeting(id=mid, title="M", owner_email=owner, status="completed", participants=[]))
        db.add(Transcript(
            id=str(uuid.uuid4()), meeting_id=mid, language="en", full_text="x",
            segments=[
                {"id": 0, "start_time": 0, "end_time": 4, "speaker": "Speaker 1", "text": SENTENCE, "confidence": 0.9},
                {"id": 1, "start_time": 4, "end_time": 7, "speaker": "Speaker 2",
                 "text": "Megapanvala will handle it, thanks Megapanvalas team.", "confidence": 0.9},
            ],
            word_count=20, speaker_count=2, speaker_voices={"Speaker 1": [1.0, 0.0], "Speaker 2": [0.0, 1.0]},
            created_at=datetime.utcnow()))
        db.add(Summary(id=str(uuid.uuid4()), meeting_id=mid, created_at=datetime.utcnow(),
                       executive_summary="Megapanvala owns validation.",
                       key_points=[{"topic": "Testing", "description": "megapanvala leads"}],
                       decisions=[{"decision": "Validate", "owner": "Megapanvala", "context": ""}],
                       next_steps=["Megapanvala to test"], key_quotes=["Megapanvala: validation"]))
        db.add(ActionItemDB(id="a1", meeting_id=mid, task="Validation by Megapanvala", assignee="Megapanvala",
                            status="pending", priority="medium"))
        db.commit()
    return mid


def get(db, mid):
    return voices.latest_transcript(db, mid)


# ----------------------------------------------------------- list_speakers

def test_unnamed_speakers_get_a_suggestion_and_named_ones_do_not(Session):
    mid = seed(Session)
    with Session() as db:
        by = {s["label"]: s for s in voices.list_speakers(get(db, mid), ["Megha Panwala"])}
        assert by["Speaker 1"]["suggestion"]["name"] == "Megha Panwala"
        assert by["Speaker 2"]["suggestion"] is None
        t = get(db, mid)
        t.segments = [{**s, "speaker": "Megha Panwala" if s["speaker"] == "Speaker 1" else s["speaker"]} for s in t.segments]
        db.commit()
        by = {s["label"]: s for s in voices.list_speakers(get(db, mid))}
        assert by["Megha Panwala"]["suggestion"] is None


# ----------------------------------------------------------- edit_segment

def test_editing_a_segment_updates_the_text_counts_and_marks_it(Session):
    mid = seed(Session)
    with Session() as db:
        out = te.edit_segment(db, db.get(Meeting, mid), 0, "  My name is   Megha Panwala and I will do validation. ")
        assert out["text"] == "My name is Megha Panwala and I will do validation." and out["edited"] and out["changed"]
        t = get(db, mid)
        assert t.segments[0]["edited"] is True and "edited" not in t.segments[1]
        assert t.full_text.startswith("My name is Megha Panwala and I will do validation. Megapanvala will")
        assert t.word_count == len(t.full_text.split())


def test_saving_unchanged_text_changes_nothing(Session):
    mid = seed(Session)
    with Session() as db:
        out = te.edit_segment(db, db.get(Meeting, mid), 0, SENTENCE)
        assert out["changed"] is False
        assert "edited" not in get(db, mid).segments[0]


def test_segment_edit_validation(Session):
    mid = seed(Session)
    with Session() as db:
        m = db.get(Meeting, mid)
        for bad, code in (("   ", 422), ("x" * 5001, 422), ("a\x00b", 422)):
            with pytest.raises(voices.VoiceError) as e:
                te.edit_segment(db, m, 0, bad)
            assert e.value.status_code == code
        with pytest.raises(voices.VoiceError) as e:
            te.edit_segment(db, m, 99, "text")
        assert e.value.status_code == 404
        db.query(Transcript).delete()
        db.commit()
        with pytest.raises(voices.VoiceError) as e:
            te.edit_segment(db, m, 0, "text")
        assert e.value.status_code == 404


# --------------------------------------------------------- replace everywhere

def test_replace_fixes_the_transcript_summary_and_tasks(Session):
    mid = seed(Session)
    with Session() as db:
        c = te.replace_everywhere(db, db.get(Meeting, mid), "Megapanvala", "Megha Panwala")
        assert c == {"transcript": 2, "summary": 5, "action_items": 2, "total": 9}
        t = get(db, mid)
        assert "Megapanvala " not in t.full_text and t.full_text.count("Megha Panwala") == 2
        assert t.segments[0]["edited"] and t.segments[1]["edited"]
        assert "Megapanvalas team" in t.segments[1]["text"]               # a different word: untouched
        s = db.query(Summary).one()
        assert s.executive_summary == "Megha Panwala owns validation."
        assert s.key_points[0]["description"] == "Megha Panwala leads"     # case-insensitive
        assert s.decisions[0]["owner"] == "Megha Panwala" and s.next_steps == ["Megha Panwala to test"]
        a = db.get(ActionItemDB, "a1")
        assert (a.task, a.assignee) == ("Validation by Megha Panwala", "Megha Panwala")


def test_replace_options(Session):
    mid = seed(Session)
    with Session() as db:
        m = db.get(Meeting, mid)
        assert te.replace_everywhere(db, m, "megapanvala", "X", match_case=True, include_summary=False)["total"] == 0
        assert te.replace_everywhere(db, m, "megapanvala", "X", match_case=True)["summary"] == 1   # only the lowercase one
        c = te.replace_everywhere(db, m, "Megapanvala", "Y", whole_word=False)
        assert c["transcript"] == 3                                        # includes "Megapanvalas"
        assert "Ys team" in get(db, mid).segments[1]["text"]


def test_replace_can_leave_summary_and_tasks_alone(Session):
    mid = seed(Session)
    with Session() as db:
        c = te.replace_everywhere(db, db.get(Meeting, mid), "Megapanvala", "Megha", include_summary=False)
        assert c["summary"] == 0 and c["action_items"] == 0 and c["transcript"] == 2
        assert db.query(Summary).one().executive_summary.startswith("Megapanvala")
        assert db.get(ActionItemDB, "a1").assignee == "Megapanvala"


def test_replace_fixes_speaker_labels_and_keeps_their_voice(Session):
    mid = seed(Session)
    with Session() as db:
        t = get(db, mid)
        t.segments = [{**s, "speaker": "Megapanvala" if s["speaker"] == "Speaker 1" else s["speaker"]} for s in t.segments]
        t.speaker_voices = {"Megapanvala": [1.0, 0.0], "Speaker 2": [0.0, 1.0]}
        db.commit()
        te.replace_everywhere(db, db.get(Meeting, mid), "Megapanvala", "Megha Panwala")
        t = get(db, mid)
        assert t.segments[0]["speaker"] == "Megha Panwala"
        assert set(t.speaker_voices) == {"Megha Panwala", "Speaker 2"}


def test_replace_treats_the_text_literally(Session):
    mid = seed(Session)
    with Session() as db:
        m = db.get(Meeting, mid)
        te.edit_segment(db, m, 1, "We use C++ (v2) and $1 prices")
        assert te.replace_everywhere(db, m, "C++ (v2)", r"Rust \1 \g<0> $1")["transcript"] == 1
        assert get(db, mid).segments[1]["text"] == r"We use Rust \1 \g<0> $1 and $1 prices"


def test_replace_validation(Session):
    mid = seed(Session)
    with Session() as db:
        m = db.get(Meeting, mid)
        for find, rep in (("", "x"), ("   ", "x"), ("a", "a"), ("x" * 201, "y"), ("a\x00", "b")):
            with pytest.raises(voices.VoiceError) as e:
                te.replace_everywhere(db, m, find, rep)
            assert e.value.status_code == 422
        assert te.replace_everywhere(db, m, "nothing like this", "x")["total"] == 0


# ------------------------------------------------------------------ the API

def test_api_edit_replace_and_permissions(client, Session):
    h = signup(client, "k@example.com")
    stranger = signup(client, "s@example.com")
    reader = signup(client, "r@example.com")
    mid = seed(Session)
    with Session() as db:
        db.get(Meeting, mid).participants = ["r@example.com"]
        db.commit()
    seg = f"/api/v1/meetings/{mid}/transcript/segments"
    rep = f"/api/v1/meetings/{mid}/transcript/replace"

    r = client.patch(f"{seg}/0", json={"text": "My name is Megha Panwala."}, headers=h)
    assert r.status_code == 200 and r.json()["edited"] is True
    shown = client.get(f"/api/v1/meetings/{mid}/transcript", headers=h).json()
    assert shown["segments"][0]["text"] == "My name is Megha Panwala." and shown["segments"][0]["edited"] is True

    assert client.patch(f"{seg}/0", json={"text": "x"}, headers=stranger).status_code == 404
    assert client.patch(f"{seg}/0", json={"text": "x"}, headers=reader).status_code == 403
    assert client.patch(f"{seg}/0", json={"text": ""}, headers=h).status_code == 422
    assert client.patch(f"{seg}/99", json={"text": "x"}, headers=h).status_code == 404
    assert client.patch(f"{seg}/0", json={"text": "x"}).status_code == 401

    r = client.post(rep, json={"find": "Megapanvala", "replace": "Megha Panwala"}, headers=h)
    assert r.status_code == 200 and r.json()["transcript"] == 1 and r.json()["summary"] == 5
    assert client.post(rep, json={"find": "a", "replace": "b"}, headers=reader).status_code == 403
    assert client.post(rep, json={"find": "a", "replace": "b"}, headers=stranger).status_code == 404
    assert client.post(rep, json={"find": "", "replace": "b"}, headers=h).status_code == 422
    assert client.post(rep, json={"find": "a", "replace": "a"}, headers=h).status_code == 422


def test_api_speakers_carry_the_suggestion_with_known_names(client, Session):
    h = signup(client, "k@example.com")
    client.patch("/auth/me", json={"name": "Megha Panwala"}, headers=h)
    mid = seed(Session)
    out = client.get(f"/api/v1/meetings/{mid}/speakers", headers=h).json()["speakers"]
    s1 = next(s for s in out if s["label"] == "Speaker 1")
    assert s1["suggestion"]["name"] == "Megha Panwala" and s1["suggestion"]["heard"] == "Megapanvala"
    assert s1["suggestion"]["known"] is True


def test_naming_a_speaker_can_fix_the_misspelling_everywhere(client, Session):
    h = signup(client, "k@example.com")
    mid = seed(Session)
    r = client.put(f"/api/v1/meetings/{mid}/speakers", headers=h,
                   json={"label": "Speaker 1", "name": "Megha Panwala", "replace_heard": "Megapanvala"})
    assert r.status_code == 200 and r.json()["text_fixed"] > 0
    t = client.get(f"/api/v1/meetings/{mid}/transcript", headers=h).json()
    assert t["segments"][0]["speaker"] == "Megha Panwala"
    assert "Megapanvala" not in t["segments"][0]["text"] and "Megha Panwala" in t["segments"][0]["text"]
    acts = client.get(f"/api/v1/meetings/{mid}/actions", headers=h).json()["action_items"]
    assert acts[0]["assignee"] == "Megha Panwala"


def test_replace_heard_is_skipped_when_it_equals_the_chosen_name(client, Session):
    h = signup(client, "k@example.com")
    mid = seed(Session)
    r = client.put(f"/api/v1/meetings/{mid}/speakers", headers=h,
                   json={"label": "Speaker 1", "name": "Megapanvala", "replace_heard": "megapanvala"})
    assert r.status_code == 200 and "text_fixed" not in r.json()
