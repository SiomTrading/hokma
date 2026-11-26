"""The transcript of a video as blocks with times (SPEC-030, A1-5)."""
import pytest

from hokma.sources import transcript
from hokma.sources.transcript import Entry, TranscriptError


def test_a_mark_in_brackets_a_plain_mark_and_a_range_all_start_a_line():
    got = transcript.parse("[00:03] first\n00:10 second\n(00:20) third\n00:25 - 00:29 fourth\n1:01:05 fifth")
    assert [(e.start, e.end, e.text) for e in got] == [(3.0, None, "first"), (10.0, None, "second"), (20.0, None, "third"), (25.0, 29.0, "fourth"),
                                                        (3665.0, None, "fifth")]


def test_a_line_without_a_mark_continues_the_entry_before_it():
    got = transcript.parse("[00:03] the stop\nis one ATR\n[00:09] and the target")
    assert [e.text for e in got] == ["the stop is one ATR", "and the target"]


def test_the_blocks_of_a_subtitle_file_are_read_without_their_numbers_and_header():
    srt = "WEBVTT\n\n1\n00:00:01,000 --> 00:00:04,000\nHello there\n\n2\n00:00:05,500 --> 00:00:09,000\nsecond line\n"
    got = transcript.parse(srt)
    assert [(e.start, e.end, e.text) for e in got] == [(1.0, 4.0, "Hello there"), (5.5, 9.0, "second line")]


@pytest.mark.parametrize("text", ["no marks at all", "text before [00:03] a mark", "[00:03]\n[00:04]   ", ""])
def test_text_with_no_usable_mark_is_refused(text):
    with pytest.raises(TranscriptError):
        transcript.parse(text)


def test_entries_are_grouped_in_blocks_of_30_to_60_seconds_with_their_range_as_locator():
    lines = [f"[{(i * 10) // 60:02d}:{(i * 10) % 60:02d}] sentence {i}" for i in range(0, 13)]        # one every 10 seconds, 0 to 120
    got = transcript.blocks(transcript.parse("\n".join(lines)))
    assert [place for place, _ in got] == ["00:00-00:30", "00:30-01:00", "01:00-01:30", "01:30-02:00", "02:00-02:05"]
    assert got[0][1] == "sentence 0 sentence 1 sentence 2" and got[4][1] == "sentence 12"


def test_a_block_ends_where_its_last_entry_ends_when_the_entry_has_an_end():
    got = transcript.blocks(transcript.parse("03:12 - 03:40 the stop is one ATR"))
    assert got == [("03:12-03:40", "the stop is one ATR")]


def test_an_hour_in_is_written_with_the_hour():
    assert transcript.clock(3725) == "1:02:05" and transcript.clock(75) == "01:15" and transcript.seconds("1:02:05") == 3725


def test_the_notes_about_the_frames_are_passages_by_time_or_one_passage_without_marks():
    assert transcript.frames("[00:12] a chart with two averages\nframe 01:05: the stop line") == [("frame 00:12", "a chart with two averages"),
                                                                                                  ("frame 01:05", "the stop line")]
    assert transcript.frames("a chart with two averages and a stop") == [("frame notes", "a chart with two averages and a stop")]
    assert transcript.frames("   ") == []


def test_an_entry_has_the_fields_the_blocks_need():
    assert Entry(1.0, None, "x").end is None


def test_a_block_of_a_very_long_entry_is_cut_in_short_pieces_that_keep_its_time_mark():
    long = "word " * 120_000
    got = transcript.blocks(transcript.parse(f"[03:12] {long}\n[03:20] the end"))
    assert len(got) > 100 and all(len(text) <= transcript.MAX_PASSAGE_CHARS for _p, text in got)
    assert all(place.startswith("03:12-") for place, _t in got) and got[0][0].endswith(f"(1/{len(got)})")
    assert "word " * 10 in got[3][1] and "the end" in got[-1][1]


def test_a_piece_shares_its_edge_with_the_next_so_a_sentence_on_the_cut_is_whole_in_one():
    text = " ".join(f"w{n}" for n in range(2_000))
    pieces = [t for _p, t in transcript.split_long("00:01-00:02", text)]
    assert all(len(p) <= transcript.MAX_PASSAGE_CHARS for p in pieces) and len(pieces) > 2
    for a, b in zip(pieces, pieces[1:]):
        assert set(a.split()[-5:]) <= set(b.split()), (a[-40:], b[:40])
    assert pieces[0].split()[0] == "w0" and pieces[-1].split()[-1] == "w1999"


def test_the_notes_about_the_frames_are_cut_too():
    got = transcript.frames("[00:12] " + "x" * 5_000)
    assert len(got) >= 4 and all(place.startswith("frame 00:12 (") and len(text) <= transcript.MAX_PASSAGE_CHARS for place, text in got)
    flat = transcript.frames("no marks " * 1_000)
    assert len(flat) > 1 and flat[0][0].startswith("frame notes (1/")
