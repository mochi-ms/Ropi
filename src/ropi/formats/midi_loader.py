"""MIDI file loader using mido with global tempo-map conversion (Qt/Win32 independent)."""

from __future__ import annotations

import bisect
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import mido

from ropi.core.errors import FormatLoadError
from ropi.domain.note import NoteEvent, SongData, TrackInfo

_DEFAULT_TEMPO_US = 500_000  # 120 BPM
_DRUM_CHANNEL = 9  # MIDI channel 10 (0-indexed)


@dataclass(frozen=True, slots=True)
class _TempoSegment:
    tick: int
    cum_us: float
    tempo_us: int


class _TempoMap:
    """Converts MIDI absolute ticks into milliseconds across tempo changes."""

    def __init__(self, ticks_per_beat: int, tempo_events: list[tuple[int, int]]) -> None:
        if ticks_per_beat <= 0:
            raise FormatLoadError("MIDI 파일의 ticks_per_beat 값이 올바르지 않습니다.")
        self._tpb = ticks_per_beat

        # Collapse duplicate tick changes keeping the latest tempo at that tick.
        by_tick: dict[int, int] = {0: _DEFAULT_TEMPO_US}
        for tick, tempo_us in sorted(tempo_events, key=lambda x: x[0]):
            if tempo_us > 0:
                by_tick[max(0, tick)] = tempo_us

        self._ticks: list[int] = []
        self._segments: list[_TempoSegment] = []

        prev_tick = 0
        cum_us = 0.0
        prev_tempo = by_tick[0]
        for tick in sorted(by_tick.keys()):
            if tick > prev_tick:
                cum_us += ((tick - prev_tick) / self._tpb) * prev_tempo
            tempo = by_tick[tick]
            self._ticks.append(tick)
            self._segments.append(_TempoSegment(tick=tick, cum_us=cum_us, tempo_us=tempo))
            prev_tick = tick
            prev_tempo = tempo

    def tick_to_ms(self, tick: int) -> int:
        if tick <= 0:
            return 0
        idx = bisect.bisect_right(self._ticks, tick) - 1
        seg = self._segments[max(0, idx)]
        us = seg.cum_us + ((tick - seg.tick) / self._tpb) * seg.tempo_us
        return max(0, round(us / 1000.0))

    def primary_bpm(self, total_ticks: int) -> float:
        if len(self._segments) == 1 or total_ticks <= 0:
            return round(60_000_000.0 / self._segments[0].tempo_us, 2)
        # Duration-weighted average BPM across the piece.
        weighted_tempo = 0.0
        total_weight = 0
        for i, seg in enumerate(self._segments):
            next_tick = self._segments[i + 1].tick if i + 1 < len(self._segments) else total_ticks
            span = max(0, next_tick - seg.tick)
            if span > 0:
                weighted_tempo += seg.tempo_us * span
                total_weight += span
        if total_weight == 0:
            return round(60_000_000.0 / self._segments[0].tempo_us, 2)
        avg_tempo = weighted_tempo / total_weight
        return round(60_000_000.0 / avg_tempo, 2)


def load_midi(path: Path, *, include_drums: bool = True) -> SongData:
    """Parse a standard MIDI file (.mid/.midi) into an immutable SongData object."""
    if not path.exists():
        raise FormatLoadError(f"MIDI 파일을 찾을 수 없습니다: {path.name}")
    try:
        mid = mido.MidiFile(str(path), clip=True)
    except (OSError, EOFError, ValueError, KeyError, IndexError, mido.KeySignatureError) as exc:
        raise FormatLoadError(f"손상되었거나 지원하지 않는 MIDI 파일입니다: {path.name}") from exc
    except Exception as exc:
        raise FormatLoadError(f"MIDI 파일을 읽을 수 없습니다 ({path.name}): {exc}") from exc

    if mid.type not in (0, 1, 2) or not mid.tracks:
        raise FormatLoadError(f"트랙 정보가 없는 손상된 MIDI 파일입니다: {path.name}")
    if mid.ticks_per_beat <= 0:
        raise FormatLoadError(f"MIDI 파일의 박자 해상도가 올바르지 않습니다: {path.name}")

    tempo_events: list[tuple[int, int]] = []
    max_tick = 0

    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += max(0, int(msg.time))
            if msg.is_meta and msg.type == "set_tempo":
                tempo_events.append((abs_tick, int(msg.tempo)))
        if abs_tick > max_tick:
            max_tick = abs_tick

    tempo_map = _TempoMap(mid.ticks_per_beat, tempo_events)

    all_notes: list[NoteEvent] = []
    track_infos: list[TrackInfo] = []

    for track_idx, track in enumerate(mid.tracks):
        abs_tick = 0
        track_name = ""
        channels_seen: set[int] = set()
        active: dict[tuple[int, int], deque[tuple[int, int]]] = defaultdict(deque)
        track_note_count = 0

        for msg in track:
            abs_tick += max(0, int(msg.time))
            if msg.is_meta:
                if msg.type == "track_name" and not track_name:
                    track_name = str(msg.name).strip()
                continue

            msg_type = getattr(msg, "type", "")
            channel = int(getattr(msg, "channel", 0))

            if msg_type == "note_on":
                channels_seen.add(channel)
                note_num = int(msg.note)
                vel = int(msg.velocity)
                if not 0 <= note_num <= 127:
                    continue
                if vel > 0:
                    active[(channel, note_num)].append((abs_tick, vel))
                else:
                    _close_note(
                        active,
                        channel,
                        note_num,
                        abs_tick,
                        track_idx,
                        tempo_map,
                        all_notes,
                        include_drums,
                    )
                    if include_drums or channel != _DRUM_CHANNEL:
                        track_note_count += 1
            elif msg_type == "note_off":
                channels_seen.add(channel)
                note_num = int(msg.note)
                if not 0 <= note_num <= 127:
                    continue
                if active[(channel, note_num)]:
                    _close_note(
                        active,
                        channel,
                        note_num,
                        abs_tick,
                        track_idx,
                        tempo_map,
                        all_notes,
                        include_drums,
                    )
                    if include_drums or channel != _DRUM_CHANNEL:
                        track_note_count += 1

        # Close any dangling notes left open at end of track.
        for (channel, note_num), queue in active.items():
            while queue:
                start_tick, vel = queue.popleft()
                if not include_drums and channel == _DRUM_CHANNEL:
                    continue
                start_ms = tempo_map.tick_to_ms(start_tick)
                end_ms = max(start_ms, tempo_map.tick_to_ms(abs_tick))
                all_notes.append(
                    NoteEvent(
                        start_ms=start_ms,
                        duration_ms=end_ms - start_ms,
                        midi_note=note_num,
                        velocity=min(127, max(1, vel)),
                        track=track_idx,
                    )
                )
                track_note_count += 1

        is_drum = channels_seen == {_DRUM_CHANNEL}
        primary_channel = min(channels_seen) if len(channels_seen) == 1 else None
        if track_note_count > 0 or track_name:
            track_infos.append(
                TrackInfo(
                    index=track_idx,
                    name=track_name or f"Track {track_idx + 1}",
                    channel=primary_channel,
                    note_count=track_note_count,
                    is_drum=is_drum,
                )
            )

    all_notes.sort(key=lambda n: (n.start_ms, n.midi_note, n.track))
    duration_ms = max((n.end_ms for n in all_notes), default=tempo_map.tick_to_ms(max_tick))
    min_note = min((n.midi_note for n in all_notes), default=None)
    max_note = max((n.midi_note for n in all_notes), default=None)

    return SongData(
        notes=tuple(all_notes),
        tracks=tuple(track_infos),
        duration_ms=duration_ms,
        bpm=tempo_map.primary_bpm(max_tick),
        min_note=min_note,
        max_note=max_note,
        source_format="mid",
        title=path.stem,
    )


def _close_note(
    active: dict[tuple[int, int], deque[tuple[int, int]]],
    channel: int,
    note_num: int,
    end_tick: int,
    track_idx: int,
    tempo_map: _TempoMap,
    out: list[NoteEvent],
    include_drums: bool,
) -> None:
    queue = active.get((channel, note_num))
    if not queue:
        return
    start_tick, vel = queue.popleft()
    if not include_drums and channel == _DRUM_CHANNEL:
        return
    start_ms = tempo_map.tick_to_ms(start_tick)
    end_ms = max(start_ms, tempo_map.tick_to_ms(end_tick))
    out.append(
        NoteEvent(
            start_ms=start_ms,
            duration_ms=end_ms - start_ms,
            midi_note=note_num,
            velocity=min(127, max(1, vel)),
            track=track_idx,
        )
    )
