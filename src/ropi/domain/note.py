"""Pure Python musical note, track, and song data models (Qt/Win32 independent)."""

from __future__ import annotations

from dataclasses import dataclass

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_BLACK_PITCH_CLASSES = frozenset({1, 3, 6, 8, 10})


def midi_note_name(midi_note: int) -> str:
    """Convert a MIDI note number (0-127) to scientific pitch notation (e.g. 60 -> C4)."""
    if not 0 <= midi_note <= 127:
        raise ValueError(f"MIDI note out of range [0, 127]: {midi_note}")
    octave = (midi_note // 12) - 1
    return f"{_NOTE_NAMES[midi_note % 12]}{octave}"


def is_black_key(midi_note: int) -> bool:
    """Return True if the MIDI note corresponds to a piano black key (sharp/flat)."""
    return (midi_note % 12) in _BLACK_PITCH_CLASSES


@dataclass(frozen=True, slots=True)
class NoteEvent:
    """Single musical note event in milliseconds."""

    start_ms: int
    duration_ms: int
    midi_note: int
    velocity: int = 80
    track: int = 0

    def __post_init__(self) -> None:
        if self.start_ms < 0:
            raise ValueError(f"start_ms must be >= 0, got {self.start_ms}")
        if self.duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {self.duration_ms}")
        if not 0 <= self.midi_note <= 127:
            raise ValueError(f"midi_note must be in 0..127, got {self.midi_note}")
        if not 0 <= self.velocity <= 127:
            raise ValueError(f"velocity must be in 0..127, got {self.velocity}")

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


@dataclass(frozen=True, slots=True)
class PedalEvent:
    """Sustain pedal (MIDI CC64) active span in milliseconds."""

    start_ms: int
    duration_ms: int
    track: int = 0

    def __post_init__(self) -> None:
        if self.start_ms < 0:
            raise ValueError(f"start_ms must be >= 0, got {self.start_ms}")
        if self.duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {self.duration_ms}")

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """Metadata for a single track inside a parsed song."""

    index: int
    name: str
    channel: int | None
    note_count: int
    is_drum: bool = False


@dataclass(frozen=True, slots=True)
class SongData:
    """Immutable musical content extracted from a song file."""

    notes: tuple[NoteEvent, ...]
    tracks: tuple[TrackInfo, ...]
    duration_ms: int
    bpm: float
    min_note: int | None
    max_note: int | None
    source_format: str = "mid"
    title: str = ""
    pedals: tuple[PedalEvent, ...] = ()

    @property
    def note_count(self) -> int:
        return len(self.notes)

    @property
    def non_drum_tracks(self) -> tuple[TrackInfo, ...]:
        return tuple(t for t in self.tracks if not t.is_drum)

    def count_out_of_range(self, min_midi: int, max_midi: int) -> int:
        """Count notes outside [min_midi, max_midi] inclusive."""
        return sum(1 for n in self.notes if n.midi_note < min_midi or n.midi_note > max_midi)
