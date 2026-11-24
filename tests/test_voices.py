"""Speaker naming and voice recognition."""

import uuid
from datetime import datetime

import pytest

from src.models.database import (ActionItemDB, Meeting, Summary, Transcript,
                                 User, VoiceProfile)
from src.models.schemas import TranscriptionResult, TranscriptSegment
from src.services import pipeline, voices
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

A = [1.0, 0.0, 0.0, 0.0]
A2 = [0.97, 0.2, 0.0, 0.0]        # same person, slightly different sample
B = [0.0, 1.0, 0.0, 0.0]
C = [0.0, 0.0, 1.0, 0.0]


def user_id(Session, email):
    with Session() as db:
        return db.query(User).filter(User.email == email).one().id


def make_meeting(Session, owner_email, voices_by_label=None, with_voices=True):
    mid = str(uuid.uuid4())
    with Session() as db:
        db.add(Meeting(id=mid, title="M", owner_email=owner_email, status="completed",
                       participants=[]))
        db.add(Transcript(
            id=str(uuid.uuid4()), meeting_id=mid, language="en", full_text="x",
            segments=[
                {"id": 0, "start_time": 0, "end_time": 5, "speaker": "Speaker 1",
                 "text": "I will complete model training by Friday", "confidence": 0.9},
                {"id": 1, "start_time": 5, "end_time": 9, "speaker": "Speaker 2",
                 "text": "I will prepare the report", "confidence": 0.9},
                {"id": 2, "start_time": 9, "end_time": 10, "speaker": "Speaker 10",
                 "text": "Hello", "confidence": 0.9},
            ],
            word_count=14, speaker_count=3,
            speaker_voices=(voices_by_label or {"Speaker 1": A, "Speaker 2": B}) if with_voices else None,
            created_at=datetime.utcnow()))
        db.add(Summary(
            id=str(uuid.uuid4()), meeting_id=mid, created_at=datetime.utcnow(),
            executive_summary="Speaker 1 will finish training; Speaker 2 the report. Speaker 10 listened.",
            key_points=[{"topic": "Training", "description": "speaker 1 owns it"}],
            decisions=[{"decision": "Train by Friday", "owner": "Speaker 1", "context": ""}],
            next_steps=["Speaker 1 will complete model training", "Speaker 2 will prepare the report"],
            key_quotes=["Speaker 2: I will prepare the report"]))
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task="Complete model training",
                            assignee="Speaker 1", status="pending", priority="medium"))
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task="Prepare report",
                            assignee="Speaker 2", status="pending", priority="medium"))
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task="Review notes",
                            assignee="TBD", status="pending", priority="low"))
        db.commit()
    return mid


# ------------------------------------------------------------- matching

def prof(name, *samples):
    return VoiceProfile(id=name, owner_id="o", display_name=name, embeddings=list(samples))


def test_cosine_basics():
    assert voices.cosine(A, A) == pytest.approx(1.0)
    assert voices.cosine(A, B) == pytest.approx(0.0)
    assert voices.cosine(A, [1, 0]) == 0.0           # length mismatch
    assert voices.cosine([0, 0, 0, 0], A) == 0.0


def test_match_names_close_voices_only():
    out = voices.match_speakers([prof("Krishna Panwala", A), prof("Rahul Patel", B)],
                                {"Speaker 1": A2, "Speaker 2": B, "Speaker 3": C}, 0.6)
    assert out == {"Speaker 1": "Krishna Panwala", "Speaker 2": "Rahul Patel"}


def test_match_never_gives_one_name_to_two_speakers():
    out = voices.match_speakers([prof("Krishna Panwala", A)],
                                {"Speaker 1": A2, "Speaker 2": A}, 0.6)
    assert out == {"Speaker 2": "Krishna Panwala"}    # the closer voice wins


def test_match_uses_best_sample_and_threshold(monkeypatch):
    p = prof("K", B, A)
    assert voices.match_speakers([p], {"S": A2}, 0.6) == {"S": "K"}
    assert voices.match_speakers([p], {"S": C}, 0.6) == {}
    monkeypatch.setenv("VOICE_MATCH_THRESHOLD", "0.99")
    assert voices.match_speakers([p], {"S": A2}) == {}


# ------------------------------------------------------------- renaming

def test_rename_rewrites_everywhere_and_saves_the_voice(Session):
    signup_client = None  # noqa: F841
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        meeting = db.get(Meeting, mid)
        out = voices.rename_speaker(db, meeting, "Speaker 1", "  Krishna   Panwala ",
                                    db.get(User, "u1"))
        assert out["renamed"] and out["voice_saved"] and out["action_items_updated"] == 1

        t = voices.latest_transcript(db, mid)
        assert [s["speaker"] for s in t.segments] == ["Krishna Panwala", "Speaker 2", "Speaker 10"]
        assert set(t.speaker_voices) == {"Krishna Panwala", "Speaker 2"}
        assert t.speaker_count == 3

        s = db.query(Summary).filter(Summary.meeting_id == mid).one()
        assert s.executive_summary.startswith("Krishna Panwala will finish")
        assert "Speaker 10 listened" in s.executive_summary          # not clobbered
        assert s.key_points[0]["description"] == "Krishna Panwala owns it"   # case-insensitive
        assert s.decisions[0]["owner"] == "Krishna Panwala"
        assert s.next_steps[0].startswith("Krishna Panwala will")
        assert s.next_steps[1].startswith("Speaker 2 will")

        items = {a.task: a.assignee for a in db.query(ActionItemDB).filter_by(meeting_id=mid)}
        assert items == {"Complete model training": "Krishna Panwala",
                         "Prepare report": "Speaker 2", "Review notes": "TBD"}

        p = db.query(VoiceProfile).one()
        assert p.display_name == "Krishna Panwala" and p.embeddings == [A] and p.owner_id == "u1"


def test_rename_without_remembering_or_without_a_voice(Session):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    legacy = make_meeting(Session, "k@example.com", with_voices=False)
    with Session() as db:
        u = db.get(User, "u1")
        assert voices.rename_speaker(db, db.get(Meeting, mid), "Speaker 2", "Rahul", u,
                                     remember_voice=False)["voice_saved"] is False
        assert voices.rename_speaker(db, db.get(Meeting, legacy), "Speaker 2", "Rahul", u)["voice_saved"] is False
        assert db.query(VoiceProfile).count() == 0                    # nothing enrolled


def test_same_person_enrolled_twice_keeps_one_profile_capped(Session):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.commit()
        for i in range(voices.MAX_SAMPLES + 2):
            voices.enroll(db, "u1", "Krishna Panwala" if i % 2 else "krishna panwala", [float(i), 1.0])
        db.commit()
        p = db.query(VoiceProfile).one()
        assert len(p.embeddings) == voices.MAX_SAMPLES
        assert p.embeddings[-1] == [float(voices.MAX_SAMPLES + 1), 1.0]


def test_rename_validation(Session):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        m, u = db.get(Meeting, mid), db.get(User, "u1")
        for bad, code in (("   ", 422), ("x" * 101, 422), ("a\x00b", 422)):
            with pytest.raises(voices.VoiceError) as e:
                voices.rename_speaker(db, m, "Speaker 1", bad, u)
            assert e.value.status_code == code
        with pytest.raises(voices.VoiceError) as e:
            voices.rename_speaker(db, m, "Speaker 99", "Bob", u)
        assert e.value.status_code == 404
        assert voices.rename_speaker(db, m, "Speaker 1", "Speaker 1", u)["renamed"] is False


def test_merging_two_clusters_into_one_name(Session):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        m, u = db.get(Meeting, mid), db.get(User, "u1")
        voices.rename_speaker(db, m, "Speaker 1", "Krishna", u)
        voices.rename_speaker(db, m, "Speaker 2", "Krishna", u)
        t = voices.latest_transcript(db, mid)
        assert [s["speaker"] for s in t.segments][:2] == ["Krishna", "Krishna"]
        assert t.speaker_count == 2
        assert t.speaker_voices["Krishna"] == A                       # existing voice kept
        assert db.query(VoiceProfile).one().embeddings == [A, B]      # both clusters learned


# ------------------------------------------------- recognised in later meetings

class VoiceTranscriber:
    def __init__(self, label_voices):
        self.label_voices = label_voices

    def transcribe(self, audio_path, language="en", identify_speakers=False, known_voices=None):
        segs = [TranscriptSegment(id=0, start_time=0, end_time=3, speaker="Speaker 1", text="Hi there"),
                TranscriptSegment(id=1, start_time=3, end_time=6, speaker="Speaker 2", text="Hello")]
        return TranscriptionResult(audio_file=audio_path, language=language, segments=segs,
                                   duration_seconds=6, word_count=3, speaker_count=2,
                                   created_at=datetime.now(), speaker_voices=self.label_voices)

    def extract_voices(self, audio_path, segments):
        return self.label_voices


def test_known_voices_are_named_in_a_new_meeting(Session, tmp_path):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Krishna Panwala", embeddings=[A]))
        db.add(VoiceProfile(id="p2", owner_id="u1", display_name="Rahul Patel", embeddings=[B]))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        meeting = db.get(Meeting, mid)
        # Speaker 1 in this recording is Rahul's voice, Speaker 2 is Krishna's: order must not matter.
        t, _ = pipeline.transcribe_step(db, meeting, str(tmp_path / "a.wav"), identify_speakers=True,
                                        transcriber=VoiceTranscriber({"Speaker 1": B, "Speaker 2": A2}))
        assert [s["speaker"] for s in t.segments] == ["Rahul Patel", "Krishna Panwala"]
        assert set(t.speaker_voices) == {"Rahul Patel", "Krishna Panwala"}


def test_unknown_voice_stays_speaker_n_and_strangers_profiles_are_not_used(Session, tmp_path):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.add(User(id="u2", email="o@example.com", password_hash="x", is_active=True))
        db.add(VoiceProfile(id="p1", owner_id="u2", display_name="Someone Else", embeddings=[A]))
        db.add(VoiceProfile(id="p2", owner_id="u1", display_name="Krishna Panwala", embeddings=[A]))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        t, _ = pipeline.transcribe_step(db, db.get(Meeting, mid), str(tmp_path / "a.wav"),
                                        identify_speakers=True,
                                        transcriber=VoiceTranscriber({"Speaker 1": A, "Speaker 2": C}))
        assert [s["speaker"] for s in t.segments] == ["Krishna Panwala", "Speaker 2"]


def test_meeting_without_owner_is_left_alone(Session, tmp_path):
    with Session() as db:
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Krishna Panwala", embeddings=[A]))
        db.commit()
    mid = make_meeting(Session, None)
    with Session() as db:
        t, _ = pipeline.transcribe_step(db, db.get(Meeting, mid), str(tmp_path / "a.wav"),
                                        identify_speakers=True,
                                        transcriber=VoiceTranscriber({"Speaker 1": A, "Speaker 2": B}))
        assert [s["speaker"] for s in t.segments] == ["Speaker 1", "Speaker 2"]


def test_voices_step_adds_embeddings_and_recognises_known_people(Session, tmp_path):
    wav = tmp_path / "m.wav"
    wav.write_bytes(b"x")
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Rahul Patel", embeddings=[B]))
        db.commit()
    mid = make_meeting(Session, "k@example.com", with_voices=False)
    with Session() as db:
        meeting = db.get(Meeting, mid)
        meeting.audio_file_path = str(wav)
        db.commit()
        out = pipeline.voices_step(db, meeting, transcriber=VoiceTranscriber({"Speaker 1": A, "Speaker 2": B2()}))
        assert out == {"voices": 2, "recognised": 1}
        t = voices.latest_transcript(db, mid)
        assert [s["speaker"] for s in t.segments][:2] == ["Speaker 1", "Rahul Patel"]
        assert t.speaker_voices["Speaker 1"] == A
        a = {x.task: x.assignee for x in db.query(ActionItemDB).filter_by(meeting_id=mid)}
        assert a["Prepare report"] == "Rahul Patel"


def B2():
    return [0.02, 0.99, 0.0, 0.0]


def test_voices_step_errors(Session, tmp_path):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", password_hash="x", is_active=True))
        db.commit()
    mid = make_meeting(Session, "k@example.com", with_voices=False)
    with Session() as db:
        meeting = db.get(Meeting, mid)
        with pytest.raises(pipeline.PipelineError):                   # no audio stored
            pipeline.voices_step(db, meeting, transcriber=VoiceTranscriber({}))
        wav = tmp_path / "m.wav"
        wav.write_bytes(b"x")
        meeting.audio_file_path = str(wav)
        with pytest.raises(pipeline.PipelineError) as e:              # nothing extracted
            pipeline.voices_step(db, meeting, transcriber=VoiceTranscriber({}))
        assert e.value.status_code == 422


# ------------------------------------------------------------------ transcriber

def test_diarize_reads_embeddings_and_falls_back_without_support():
    from pyannote.core import Annotation, Segment
    from src.agent.transcriber import MeetingTranscriber

    ann = Annotation()
    ann[Segment(0, 5)] = "S_A"
    ann[Segment(5, 9)] = "S_B"

    class WithEmb:
        def __call__(self, path, return_embeddings=False):
            return ann, [[1.0, 0.0], [0.0, 1.0]]

    d, v = MeetingTranscriber._diarize(WithEmb(), "x.wav")
    assert d is ann and v == {"S_A": [1.0, 0.0], "S_B": [0.0, 1.0]}

    class Plain:
        def __call__(self, path):
            return ann

    d, v = MeetingTranscriber._diarize(Plain(), "x.wav")
    assert d is ann and v == {}

    class WithNaN:
        def __call__(self, path, return_embeddings=False):
            return ann, [[float("nan"), 0.0], [0.0, 1.0]]

    assert MeetingTranscriber._diarize(WithNaN(), "x.wav")[1] == {"S_B": [0.0, 1.0]}


def test_extract_voices_maps_clusters_to_existing_labels(tmp_path, monkeypatch):
    from pyannote.core import Annotation, Segment
    from src.agent.transcriber import MeetingTranscriber

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    ann = Annotation()
    ann[Segment(0, 5)] = "RAW_X"
    ann[Segment(5, 10)] = "RAW_Y"

    class P:
        def __call__(self, path, return_embeddings=False):
            return ann, [[1.0, 0.0], [0.0, 1.0]]

    t = MeetingTranscriber.__new__(MeetingTranscriber)
    monkeypatch.setattr(t, "_load_diarization_pipeline", lambda: P(), raising=False)
    segs = [{"start_time": 0, "end_time": 5, "speaker": "Krishna Panwala"},
            {"start_time": 5, "end_time": 10, "speaker": "Speaker 2"},
            {"start_time": 10, "end_time": 11, "speaker": None}]
    assert t.extract_voices(str(wav), segs) == {"Krishna Panwala": [1.0, 0.0], "Speaker 2": [0.0, 1.0]}


# ------------------------------------------------------------------------ API

def test_speaker_api_flow_and_isolation(client, Session):
    h = signup(client, "k@example.com")
    h2 = signup(client, "stranger@example.com")
    mid = make_meeting(Session, "k@example.com")

    r = client.get(f"/api/v1/meetings/{mid}/speakers", headers=h)
    assert r.status_code == 200 and r.json()["voices_available"] is True
    by = {s["label"]: s for s in r.json()["speakers"]}
    assert by["Speaker 1"]["has_voice"] and not by["Speaker 1"]["named"] and by["Speaker 1"]["words"] == 7

    url = f"/api/v1/meetings/{mid}/speakers"
    assert client.put(url, json={"label": "Speaker 1", "name": "Krishna Panwala"}, headers=h2).status_code == 404
    assert client.get(url, headers=h2).status_code == 404
    assert client.put(url, json={"label": "Speaker 1", "name": "  "}, headers=h).status_code == 422
    assert client.put(url, json={"label": "Nope", "name": "X"}, headers=h).status_code == 404
    assert client.put(url, json={"name": "X"}, headers=h).status_code == 422

    r = client.put(url, json={"label": "Speaker 1", "name": "Krishna Panwala"}, headers=h)
    assert r.status_code == 200 and r.json()["voice_saved"] is True
    r = client.put(url, json={"label": "Speaker 2", "name": "Rahul Patel", "remember_voice": False}, headers=h)
    assert r.json()["voice_saved"] is False

    names = {s["label"]: s["named"] for s in client.get(url, headers=h).json()["speakers"]}
    assert names["Krishna Panwala"] and names["Rahul Patel"] and not names["Speaker 10"]
    tr = client.get(f"/api/v1/meetings/{mid}/transcript", headers=h).json()
    assert tr["speaker_count"] == 3
    acts = client.get(f"/api/v1/meetings/{mid}/actions", headers=h).json()["action_items"]
    assert {a["assignee"] for a in acts} == {"Krishna Panwala", "Rahul Patel", "TBD"}

    # profiles: listed for the owner only, never exposing embeddings
    mine = client.get("/api/v1/voice-profiles", headers=h).json()
    assert [p["name"] for p in mine["profiles"]] == ["Krishna Panwala"]
    assert "embeddings" not in mine["profiles"][0]
    assert client.get("/api/v1/voice-profiles", headers=h2).json()["count"] == 0
    pid = mine["profiles"][0]["id"]
    assert client.delete(f"/api/v1/voice-profiles/{pid}", headers=h2).status_code == 404
    assert client.delete(f"/api/v1/voice-profiles/{pid}", headers=h).status_code == 204
    assert client.get("/api/v1/voice-profiles", headers=h).json()["count"] == 0


def test_analyze_endpoint_needs_audio_and_a_transcript(client, Session):
    h = signup(client, "k@example.com")
    mid = make_meeting(Session, "k@example.com", with_voices=False)
    r = client.post(f"/api/v1/meetings/{mid}/speakers/analyze", headers=h)
    assert r.status_code == 400 and "audio" in r.json()["detail"]
    assert client.post("/api/v1/meetings/nope/speakers/analyze", headers=h).status_code == 404


# ------------------------------------------- e-mail saved with a voice (auto-share)

def two_users(Session):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", name="Krishna Panwala", password_hash="x", is_active=True))
        db.commit()


def participants(Session, mid):
    with Session() as db:
        return db.get(Meeting, mid).participants


def test_email_is_saved_with_the_voice_and_shares_the_meeting(Session):
    two_users(Session)
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        out = voices.rename_speaker(db, db.get(Meeting, mid), "Speaker 2", "Rahul Patel",
                                    db.get(User, "u1"), email="rahul@example.com")
        assert out["invited"] == "rahul@example.com"
        assert db.query(VoiceProfile).one().email == "rahul@example.com"
    assert participants(Session, mid) == ["rahul@example.com"]


def test_a_recognised_voice_shares_new_meetings_automatically(Session, tmp_path):
    two_users(Session)
    with Session() as db:
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Rahul Patel", embeddings=[B], email="rahul@example.com"))
        db.add(VoiceProfile(id="p2", owner_id="u1", display_name="No Email", embeddings=[C]))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        pipeline.transcribe_step(db, db.get(Meeting, mid), str(tmp_path / "a.wav"), identify_speakers=True,
                                 transcriber=VoiceTranscriber({"Speaker 1": B, "Speaker 2": C}))
    assert participants(Session, mid) == ["rahul@example.com"]           # "No Email" adds nothing


def test_an_unrecognised_voice_shares_nothing(Session, tmp_path):
    two_users(Session)
    with Session() as db:
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Rahul Patel", embeddings=[B], email="rahul@example.com"))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        pipeline.transcribe_step(db, db.get(Meeting, mid), str(tmp_path / "a.wav"), identify_speakers=True,
                                 transcriber=VoiceTranscriber({"Speaker 1": A, "Speaker 2": C}))
    assert participants(Session, mid) == []


def test_naming_a_speaker_with_a_known_name_reuses_the_saved_email(Session):
    two_users(Session)
    with Session() as db:
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Rahul Patel", embeddings=[B], email="rahul@example.com"))
        db.commit()
    mid = make_meeting(Session, "k@example.com")
    with Session() as db:
        out = voices.rename_speaker(db, db.get(Meeting, mid), "Speaker 2", "rahul patel", db.get(User, "u1"))
        assert out["invited"] == "rahul@example.com"
    assert participants(Session, mid) == ["rahul@example.com"]


def test_voices_step_shares_with_recognised_people(Session, tmp_path):
    two_users(Session)
    wav = tmp_path / "m.wav"
    wav.write_bytes(b"x")
    with Session() as db:
        db.add(VoiceProfile(id="p1", owner_id="u1", display_name="Rahul Patel", embeddings=[B], email="rahul@example.com"))
        db.commit()
    mid = make_meeting(Session, "k@example.com", with_voices=False)
    with Session() as db:
        m = db.get(Meeting, mid)
        m.audio_file_path = str(wav)
        db.commit()
        pipeline.voices_step(db, m, transcriber=VoiceTranscriber({"Speaker 1": A, "Speaker 2": B2()}))
    assert participants(Session, mid) == ["rahul@example.com"]


def test_sharing_skips_the_owner_and_duplicates(Session):
    m = Meeting(id="m", title="x", owner_email="K@Example.com", participants=["rahul@example.com"])
    assert voices.share_with(m, ["k@example.com", "RAHUL@example.com", None, "", " new@example.com "]) == ["new@example.com"]
    assert m.participants == ["rahul@example.com", "new@example.com"]


def test_profile_email_api(client, Session):
    h = signup(client, "k@example.com")
    h2 = signup(client, "other@example.com")
    uid = user_id(Session, "k@example.com")
    with Session() as db:
        db.add(VoiceProfile(id="p1", owner_id=uid, display_name="Rahul Patel", embeddings=[B]))
        db.commit()
    url = "/api/v1/voice-profiles/p1"
    assert client.get("/api/v1/voice-profiles", headers=h).json()["profiles"][0]["email"] is None
    r = client.patch(url, json={"email": " Rahul@Example.COM "}, headers=h)
    assert r.status_code == 200 and r.json()["email"] == "rahul@example.com"
    assert client.get("/api/v1/voice-profiles", headers=h).json()["profiles"][0]["email"] == "rahul@example.com"
    assert client.patch(url, json={"email": "nope"}, headers=h).status_code == 422
    assert client.patch(url, json={"email": "x@y.com"}, headers=h2).status_code == 404       # not theirs
    assert client.patch(url, json={"email": "x@y.com"}).status_code == 401
    assert client.patch(url, json={"email": ""}, headers=h).json()["email"] is None           # cleared
    assert client.patch(url, json={}, headers=h).json()["email"] is None


# ---------------------------------------------- hearing who an unnamed speaker is

def _t(segments):
    return Transcript(id="t", meeting_id="m", language="en", full_text="x", word_count=1, speaker_count=2,
                      segments=segments, created_at=datetime.utcnow())


def _seg(i, start, end, speaker, text="hello there"):
    return {"id": i, "start_time": start, "end_time": end, "speaker": speaker, "text": text}


def test_each_speaker_gets_their_longest_turns_as_clips_in_time_order():
    t = _t([_seg(0, 0, 2, "Speaker 1", "short"), _seg(1, 2, 12, "Speaker 2", "long one"),
            _seg(2, 12, 18, "Speaker 1", "medium"), _seg(3, 18, 19, "Speaker 1", "tiny"),
            _seg(4, 19, 25, "Speaker 1", "medium two"), _seg(5, 25, 30, "Speaker 1", "five")])
    by = {s["label"]: s for s in voices.list_speakers(t)}
    assert [c["text"] for c in by["Speaker 1"]["clips"]] == ["medium", "medium two", "five"]   # 3 longest, by time
    assert by["Speaker 2"]["clips"] == [{"start": 2.0, "end": 12.0, "text": "long one"}]


def test_a_very_long_turn_is_cut_to_a_short_clip():
    clip = voices.list_speakers(_t([_seg(0, 5, 125, "Speaker 1")]))[0]["clips"][0]
    assert (clip["start"], clip["end"]) == (5.0, 5.0 + voices.CLIP_MAX_SECONDS)


def test_empty_or_backwards_turns_are_not_offered():
    t = _t([_seg(0, 4, 4, "Speaker 1"), _seg(1, 9, 8, "Speaker 1")])
    assert voices.list_speakers(t)[0]["clips"] == []
