"""Near-match voice suggestions, recording your own voice, and recognising participants who did."""

import uuid
from datetime import datetime

import pytest

from src.agent.transcriber import DiarizationUnavailableError, NoSpeechError
from src.api import main
from src.models.database import Meeting, Transcript, User, VoiceProfile
from src.services import voices
from tests.conftest import TEST_WAV
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

A = [1.0, 0.0, 0.0, 0.0]
CLOSE_A = [0.55, 0.0, 0.835, 0.0]      # cosine 0.55 with A, and 0 with B: "possibly"
NEAR_A = [0.9, 0.436, 0.0, 0.0]        # cosine ~0.90 with A: "likely"
FAR = [0.3, 0.0, 0.0, 0.954]           # cosine 0.30 with A, 0 with B: unrelated
B = [0.0, 1.0, 0.0, 0.0]


def prof(name, *samples, email=None):
    return voices._Person(name, list(samples), email)


# ------------------------------------------------------------------ best_match

def test_a_close_voice_is_offered_with_how_sure_it_is():
    people = [prof("Rahul Patel", A), prof("Anna Larsen", B)]
    assert voices.best_match(people, NEAR_A) == {"name": "Rahul Patel", "score": 0.9, "strong": True}
    possible = voices.best_match(people, CLOSE_A)
    assert possible["name"] == "Rahul Patel" and possible["strong"] is False and 0.5 <= possible["score"] < 0.6
    assert voices.best_match(people, FAR) is None                      # too different: nothing is suggested
    assert voices.best_match([], A) is None


def test_the_offer_threshold_can_be_tuned(monkeypatch):
    people = [prof("Rahul Patel", A)]
    monkeypatch.setenv("VOICE_CONFIRM_MIN", "0.25")
    assert voices.best_match(people, FAR)["name"] == "Rahul Patel"
    monkeypatch.setenv("VOICE_CONFIRM_MIN", "0.95")
    assert voices.best_match(people, NEAR_A) is None
    monkeypatch.setenv("VOICE_CONFIRM_MIN", "junk")
    assert voices.best_match(people, NEAR_A)["name"] == "Rahul Patel"   # falls back to the default


def transcript(speakers, with_voices=True):
    segs = [{"id": i, "start_time": i * 5.0, "end_time": i * 5.0 + 4, "speaker": s, "text": f"words {i}"} for i, s in enumerate(speakers)]
    return Transcript(id="t", meeting_id="m", language="en", full_text="x", segments=segs, word_count=2, speaker_count=len(set(speakers)),
                      speaker_voices={"Speaker 1": CLOSE_A, "Speaker 2": FAR, "Anna Larsen": B} if with_voices else None,
                      created_at=datetime.utcnow())


def test_only_unnamed_speakers_with_a_saved_voice_get_a_voice_match():
    people = [prof("Rahul Patel", A), prof("Anna Larsen", B)]
    by = {s["label"]: s for s in voices.list_speakers(transcript(["Speaker 1", "Speaker 2", "Anna Larsen"]), people=people)}
    assert by["Speaker 1"]["voice_match"]["name"] == "Rahul Patel"
    assert by["Speaker 2"]["voice_match"] is None                       # nobody it sounds like
    assert by["Anna Larsen"]["voice_match"] is None                     # already named
    old = {s["label"]: s for s in voices.list_speakers(transcript(["Speaker 1"], with_voices=False), people=people)}
    assert old["Speaker 1"]["voice_match"] is None                      # no voice data (an older meeting)
    assert voices.list_speakers(transcript(["Speaker 1"]))[0]["voice_match"] is None        # no one known


# ------------------------------------------------------------- whose voices count

@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"), "x": signup(client, "x@example.com")}
    for who, name in (("k", "Krishna Panwala"), ("r", "Rahul Patel"), ("x", "Stranger")):
        client.patch("/auth/me", json={"name": name}, headers=h[who])
    with Session() as db:
        db.add(Meeting(id="m", title="M", owner_email="k@example.com", status="uploaded", participants=["r@example.com"], created_at=datetime.utcnow()))
        db.commit()
    return type("W", (), dict(h=h, client=client, Session=Session))


def uid(db, email):
    return db.query(User).filter(User.email == email).one().id


def test_a_participants_own_recording_of_their_voice_is_recognised_in_the_owners_meetings(world, monkeypatch):
    monkeypatch.setenv("VOICE_MATCH_SCOPE", "participants")
    with world.Session() as db:
        db.add(VoiceProfile(id="own", owner_id=uid(db, "k@example.com"), display_name="Krishna Panwala", embeddings=[B], email="k@example.com"))
        db.add(VoiceProfile(id="self", owner_id=uid(db, "r@example.com"), display_name="Rahul Patel", embeddings=[A], email="r@example.com"))
        # r's note about a third person is r's business: it is not used for k's meetings
        db.add(VoiceProfile(id="rs-note", owner_id=uid(db, "r@example.com"), display_name="Boss Person", embeddings=[FAR], email=None))
        # nor is a stranger's, even of someone with r's name
        db.add(VoiceProfile(id="stranger", owner_id=uid(db, "x@example.com"), display_name="Rahul Patel", embeddings=[B], email="x@example.com"))
        db.commit()
        people = {p.display_name: p for p in voices.candidate_profiles(db, db.get(Meeting, "m"))}
    assert sorted(people) == ["Krishna Panwala", "Rahul Patel"] and people["Rahul Patel"].embeddings == [A]


def test_samples_of_the_same_person_from_both_accounts_are_combined(world):
    with world.Session() as db:
        db.add(VoiceProfile(id="a", owner_id=uid(db, "k@example.com"), display_name="Rahul Patel", embeddings=[NEAR_A], email=None))
        db.add(VoiceProfile(id="b", owner_id=uid(db, "r@example.com"), display_name="rahul patel", embeddings=[A], email="r@example.com"))
        db.commit()
        got = voices.candidate_profiles(db, db.get(Meeting, "m"))
    assert len(got) == 1 and got[0].embeddings == [NEAR_A, A] and got[0].email == "r@example.com"


def test_a_meeting_with_no_owner_has_no_voices_to_recognise(world):
    with world.Session() as db:
        db.add(Meeting(id="legacy", title="L", owner_email=None, status="uploaded", participants=[], created_at=datetime.utcnow()))
        db.commit()
        assert voices.candidate_profiles(db, db.get(Meeting, "legacy")) == []


def test_new_recordings_name_a_participant_from_their_own_voice(world, monkeypatch):
    monkeypatch.setenv("VOICE_MATCH_SCOPE", "participants")
    from src.services import pipeline
    from tests.test_voices import VoiceTranscriber
    with world.Session() as db:
        db.add(VoiceProfile(id="self", owner_id=uid(db, "r@example.com"), display_name="Rahul Patel", embeddings=[A], email="r@example.com"))
        db.commit()
        t, _ = pipeline.transcribe_step(db, db.get(Meeting, "m"), "x.wav", identify_speakers=True,
                                        transcriber=VoiceTranscriber({"Speaker 1": NEAR_A, "Speaker 2": FAR}))
        assert [s["speaker"] for s in t.segments] == ["Rahul Patel", "Speaker 2"]


# ----------------------------------------- every member's voice is checked (default)

def _everyone(db):
    """k owns the meeting; r (listed) and x (not listed) each saved their own voice; k has a private note too."""
    db.add(VoiceProfile(id="own", owner_id=uid(db, "k@example.com"), display_name="Krishna Panwala", embeddings=[B], email="k@example.com"))
    db.add(VoiceProfile(id="self-r", owner_id=uid(db, "r@example.com"), display_name="Rahul Patel", embeddings=[A], email="r@example.com"))
    db.add(VoiceProfile(id="self-x", owner_id=uid(db, "x@example.com"), display_name="Stranger", embeddings=[FAR], email="x@example.com"))
    # a note r made about somebody else (no e-mail of its own) is r's business
    db.add(VoiceProfile(id="note", owner_id=uid(db, "r@example.com"), display_name="Boss Person", embeddings=[CLOSE_A], email=None))
    db.commit()


def test_every_members_own_voice_is_checked_without_listing_them(world):
    with world.Session() as db:
        _everyone(db)
        names = sorted(p.display_name for p in voices.candidate_profiles(db, db.get(Meeting, "m")))
    assert names == ["Krishna Panwala", "Rahul Patel", "Stranger"]


def test_the_scope_can_be_narrowed_back_to_listed_people(world, monkeypatch):
    monkeypatch.setenv("VOICE_MATCH_SCOPE", "participants")
    with world.Session() as db:
        _everyone(db)
        names = sorted(p.display_name for p in voices.candidate_profiles(db, db.get(Meeting, "m")))
    assert names == ["Krishna Panwala", "Rahul Patel"]


def test_a_deactivated_member_is_not_recognised(world):
    with world.Session() as db:
        _everyone(db)
        db.query(User).filter(User.email == "x@example.com").update({"is_active": False})
        db.commit()
        names = sorted(p.display_name for p in voices.candidate_profiles(db, db.get(Meeting, "m")))
    assert "Stranger" not in names


def test_a_recognised_member_who_was_not_listed_is_named_and_given_access(world):
    from src.services import pipeline
    from tests.test_voices import VoiceTranscriber
    with world.Session() as db:
        _everyone(db)
        meeting = db.get(Meeting, "m")
        meeting.participants = []
        db.commit()
        t, _ = pipeline.transcribe_step(db, meeting, "x.wav", identify_speakers=True,
                                        transcriber=VoiceTranscriber({"Speaker 1": NEAR_A, "Speaker 2": [0.0, 0.0, 1.0, 0.0]}))
        db.commit()
        assert [s["speaker"] for s in t.segments] == ["Rahul Patel", "Speaker 2"]
        assert db.get(Meeting, "m").participants == ["r@example.com"]


def test_the_people_directory_lists_named_members(world):
    out = world.client.get("/api/v1/people", headers=world.h["k"]).json()
    assert {"name": "Rahul Patel", "email": "r@example.com"} in out["people"]
    assert world.client.get("/api/v1/people").status_code == 401


def test_a_member_is_found_by_name_only_when_that_is_unambiguous(world):
    with world.Session() as db:
        assert voices.email_for_name(db, "  rahul   PATEL ") == "r@example.com"
        assert voices.email_for_name(db, "Nobody") is None
        db.query(User).filter(User.email == "x@example.com").update({"name": "Rahul Patel"})
        db.commit()
        assert voices.email_for_name(db, "Rahul Patel") is None


# --------------------------------------------- correcting a wrong match

def _named_meeting(world, who="Rahul Patel", participants=("r@example.com",)):
    with world.Session() as db:
        m = db.get(Meeting, "m")
        m.participants = list(participants)
        db.add(Transcript(id="t", meeting_id="m", language="en", full_text="x", word_count=2, speaker_count=1, created_at=datetime.utcnow(),
                          speaker_voices={who: A},
                          segments=[{"id": 0, "start_time": 0, "end_time": 3, "speaker": who, "text": "hi"}]))
        db.commit()


def test_a_wrong_match_can_be_changed_to_another_member_and_their_email_is_filled_in(world):
    _named_meeting(world)
    r = world.client.put("/api/v1/meetings/m/speakers", headers=world.h["k"], json={"label": "Rahul Patel", "name": "Stranger"})
    assert r.status_code == 200 and r.json()["invited"] == "x@example.com" and r.json()["revoked"] is None
    with world.Session() as db:
        # the wrongly named person keeps access unless asked: they were listed, maybe on purpose
        assert db.get(Meeting, "m").participants == ["r@example.com", "x@example.com"]


def test_the_wrongly_named_persons_access_can_be_removed_with_the_correction(world):
    _named_meeting(world)
    r = world.client.put("/api/v1/meetings/m/speakers", headers=world.h["k"],
                         json={"label": "Rahul Patel", "name": "Stranger", "revoke_previous": True})
    assert r.json()["revoked"] == "r@example.com"
    with world.Session() as db:
        assert db.get(Meeting, "m").participants == ["x@example.com"]
    # r can no longer open it
    assert world.client.get("/api/v1/meetings/m", headers=world.h["r"]).status_code in (403, 404)


def test_a_correction_can_name_someone_who_is_not_a_member_with_an_email(world):
    _named_meeting(world)
    r = world.client.put("/api/v1/meetings/m/speakers", headers=world.h["k"],
                         json={"label": "Rahul Patel", "name": "Guest Person", "email": "guest@example.com", "revoke_previous": True})
    assert r.json()["invited"] == "guest@example.com" and r.json()["revoked"] == "r@example.com"
    with world.Session() as db:
        assert db.get(Meeting, "m").participants == ["guest@example.com"]


def test_removing_access_never_removes_the_owner(world):
    _named_meeting(world, who="Krishna Panwala", participants=())
    r = world.client.put("/api/v1/meetings/m/speakers", headers=world.h["k"],
                         json={"label": "Krishna Panwala", "name": "Stranger", "revoke_previous": True})
    assert r.json()["revoked"] is None
    with world.Session() as db:
        assert db.get(Meeting, "m").owner_email == "k@example.com"


def test_the_speakers_endpoint_says_who_a_named_speaker_is(world):
    _named_meeting(world)
    out = world.client.get("/api/v1/meetings/m/speakers", headers=world.h["k"]).json()["speakers"]
    assert out[0]["label"] == "Rahul Patel" and out[0]["email"] == "r@example.com"


# ------------------------------------------------------------------- the API

def test_the_speakers_endpoint_offers_the_voice_match(world):
    with world.Session() as db:
        db.add(VoiceProfile(id="p", owner_id=uid(db, "k@example.com"), display_name="Rahul Patel", embeddings=[A]))
        db.add(Transcript(id="t", meeting_id="m", language="en", full_text="x", word_count=2, speaker_count=2, created_at=datetime.utcnow(),
                          speaker_voices={"Speaker 1": CLOSE_A, "Speaker 2": FAR},
                          segments=[{"id": 0, "start_time": 0, "end_time": 3, "speaker": "Speaker 1", "text": "hi"},
                                    {"id": 1, "start_time": 3, "end_time": 6, "speaker": "Speaker 2", "text": "ho"}]))
        db.commit()
    out = world.client.get("/api/v1/meetings/m/speakers", headers=world.h["k"]).json()["speakers"]
    by = {s["label"]: s for s in out}
    assert by["Speaker 1"]["voice_match"]["name"] == "Rahul Patel" and by["Speaker 1"]["voice_match"]["strong"] is False
    assert by["Speaker 2"]["voice_match"] is None
    # saying yes is just naming the speaker; the voice sample is added to the person's profile
    r = world.client.put("/api/v1/meetings/m/speakers", headers=world.h["k"], json={"label": "Speaker 1", "name": "Rahul Patel"})
    assert r.json()["voice_saved"] is True
    with world.Session() as db:
        assert len(db.get(VoiceProfile, "p").embeddings) == 2


# ----------------------------------------------------------- record my voice

@pytest.fixture
def mic(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path / "audio"))
    box = type("Mic", (), {"calls": [], "result": {"embedding": A, "speech_seconds": 5.2}, "error": None})()

    def fake(path, **kw):
        box.calls.append(path)
        import os
        assert os.path.exists(path)                       # the clip is there while it is analysed
        if box.error:
            raise box.error
        return box.result

    monkeypatch.setattr(main.transcriber, "extract_single_voice", fake)
    box.dir = tmp_path / "audio"
    return box


def record(w, who="r", data=None, name="me.wav"):
    data = open(TEST_WAV, "rb").read() if data is None else data
    return w.client.post("/api/v1/voice-profiles/me", files={"audio_file": (name, data)}, headers=w.h[who])


def test_recording_my_voice_saves_it_under_my_name_and_email(world, mic):
    r = record(world)
    assert r.status_code == 201
    d = r.json()
    assert d["name"] == "Rahul Patel" and d["email"] == "r@example.com" and d["samples"] == 1 and d["speech_seconds"] == 5.2
    with world.Session() as db:
        p = db.query(VoiceProfile).one()
        assert p.owner_id == uid(db, "r@example.com") and p.embeddings == [A]
    assert record(world).json()["samples"] == 2                       # recording again adds a sample, not a second profile
    assert world.client.get("/api/v1/voice-profiles", headers=world.h["r"]).json()["count"] == 1


def test_the_clip_is_deleted_after_analysis(world, mic):
    record(world)
    record(world)
    assert not mic.dir.exists() or [p for p in mic.dir.rglob("*") if p.is_file()] == []


def test_recording_my_voice_needs_a_name_and_a_sensible_clip(world, mic):
    world.client.post("/auth/register", json={"email": "n@example.com", "password": PASSWORD})
    tok = world.client.post("/auth/login", json={"email": "n@example.com", "password": PASSWORD}).json()["access_token"]
    nameless = world.client.post("/api/v1/voice-profiles/me", files={"audio_file": ("a.wav", open(TEST_WAV, "rb").read())}, headers={"Authorization": f"Bearer {tok}"})
    assert nameless.status_code == 422 and "full name" in nameless.json()["detail"]
    assert record(world, name="clip.txt").status_code == 415
    assert record(world, data=b"").status_code == 400
    assert record(world, data=b"definitely not audio", name="x.wav").status_code == 400
    assert mic.calls == []


def test_clips_that_are_too_short_or_too_long_are_refused(world, mic, monkeypatch):
    from src.services import audio_storage
    monkeypatch.setattr(audio_storage, "probe_duration", lambda p: 1.2)
    assert "too short" in record(world).json()["detail"]
    monkeypatch.setattr(audio_storage, "probe_duration", lambda p: 400.0)
    assert "too long" in record(world).json()["detail"]
    monkeypatch.setattr(main, "MY_VOICE_MAX_BYTES", 100)
    assert record(world).status_code == 413
    assert mic.calls == []


def test_the_voice_must_be_one_clear_voice(world, mic):
    mic.error = NoSpeechError("More than one voice was heard. Record again in a quiet place, on your own.")
    r = record(world)
    assert r.status_code == 422 and "More than one voice" in r.json()["detail"]
    mic.error = DiarizationUnavailableError("Speaker diarization requires HF_TOKEN.")
    assert record(world).status_code == 503
    with world.Session() as db:
        assert db.query(VoiceProfile).count() == 0


def test_recording_my_voice_needs_signing_in(world, mic):
    assert world.client.post("/api/v1/voice-profiles/me", files={"audio_file": ("a.wav", b"x")}).status_code == 401
    assert world.client.post("/api/v1/voice-profiles/me", headers=world.h["r"]).status_code == 422


def test_turn_matching_names_people_the_diarizer_merged():
    """Two people the diarizer put in one speaker are told apart by matching each turn to the saved voices."""
    import numpy as np
    from types import SimpleNamespace
    from src.agent.transcriber import MeetingTranscriber
    rng = np.random.default_rng(1)
    base = {n: rng.normal(size=16) for n in ("Ann", "Bob", "Cy", "Di")}
    known = {n: [v.tolist()] for n, v in base.items()}
    spoken = ["Ann", "Bob", "Ann", "Cy", "Di", "Bob", "Cy", "Di", "Ann", "Bob"]
    turns = [(i * 3.0, i * 3.0 + 2.5, "SPEAKER_00") for i in range(len(spoken))]   # all merged by the diarizer
    voices_by_start = iter(base[n] + rng.normal(scale=0.2, size=16) for n in spoken)

    class Audio:
        def crop(self, path, seg, mode=None):
            import torch
            return torch.zeros(1, 100), 16000

    class Embed:
        def __call__(self, wave):
            return np.asarray(next(voices_by_start))[None]

    t = MeetingTranscriber.__new__(MeetingTranscriber)
    out, voices_ = t._name_turns(SimpleNamespace(_audio=Audio(), _embedding=Embed()), "x.wav", turns,
                                 {"SPEAKER_00": [0.0] * 16}, known)
    assert [l.replace("known:", "") for _, _, l in out] == spoken
