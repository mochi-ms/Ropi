"""Virtual piano keymap profiles and Windows Set-1 hardware scan codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Modifier = Literal["none", "shift", "ctrl"]

# Windows Set-1 hardware scan codes (verified with MapVirtualKeyW).
KEY_SCANCODES: dict[str, int] = {
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
    "9": 0x0A,
    "0": 0x0B,
    "q": 0x10,
    "w": 0x11,
    "e": 0x12,
    "r": 0x13,
    "t": 0x14,
    "y": 0x15,
    "u": 0x16,
    "i": 0x17,
    "o": 0x18,
    "p": 0x19,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "f": 0x21,
    "g": 0x22,
    "h": 0x23,
    "j": 0x24,
    "k": 0x25,
    "l": 0x26,
    "z": 0x2C,
    "x": 0x2D,
    "c": 0x2E,
    "v": 0x2F,
    "b": 0x30,
    "n": 0x31,
    "m": 0x32,
}

MODIFIER_SCANCODES: dict[str, int] = {
    "shift": 0x2A,  # Left Shift
    "ctrl": 0x1D,  # Left Ctrl
    "space": 0x39,  # Sustain pedal
}

# Standard 61-key Roblox Virtual Piano layout (C2=36 .. C7=96)
VP_61_CHARS = "1!2@34$5%6^78*9(0qQwWeErtTyYuiIoOpPasSdDfgGhHjJklLzZxcCvVbBnm"
_SHIFTED_TO_BASE: dict[str, str] = {
    "!": "1",
    "@": "2",
    "$": "4",
    "%": "5",
    "^": "6",
    "*": "8",
    "(": "9",
    **{c.upper(): c for c in "qwetyiopsdghjlzcvb"},
}

# 88-key extensions (A0=21..B1=35 and C#7=97..C8=108), Ctrl + base key
VP_88_LOW_KEYS = "1234567890qwert"
VP_88_HIGH_KEYS = "yuiopasdfghj"


class KeyBinding(BaseModel):
    """Mapping from a single MIDI note to a physical key + modifier."""

    midi_note: int = Field(ge=0, le=127)
    key: str = Field(min_length=1, max_length=8)
    base_key: str = Field(min_length=1, max_length=2)
    scancode: int = Field(ge=1, le=255)
    modifier: Modifier = "none"
    extended: bool = False


class KeymapProfile(BaseModel):
    """Serializable keymap profile loaded from assets/keymaps/*.json."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    key_count: int = Field(ge=1, le=128)
    base_midi_note: int = Field(default=36, ge=0, le=127)
    min_midi_note: int = Field(ge=0, le=127)
    max_midi_note: int = Field(ge=0, le=127)
    bindings: list[KeyBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_bindings(self) -> KeymapProfile:
        if self.min_midi_note > self.max_midi_note:
            raise ValueError("min_midi_note cannot exceed max_midi_note")
        notes = [b.midi_note for b in self.bindings]
        if len(notes) != len(set(notes)):
            raise ValueError("Duplicate midi_note in bindings")
        return self


class Keymap:
    """Fast lookup wrapper around a validated KeymapProfile."""

    def __init__(self, profile: KeymapProfile) -> None:
        self.profile = profile
        self._by_note: dict[int, KeyBinding] = {b.midi_note: b for b in profile.bindings}
        self._by_char: dict[str, KeyBinding] = {}
        for b in profile.bindings:
            if b.modifier != "ctrl":
                self._by_char[b.key] = b

    @property
    def id(self) -> str:
        return self.profile.id

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def min_note(self) -> int:
        return self.profile.min_midi_note

    @property
    def max_note(self) -> int:
        return self.profile.max_midi_note

    def resolve(self, midi_note: int) -> KeyBinding | None:
        """Look up the key binding for a MIDI note, or None if out of range."""
        return self._by_note.get(midi_note)

    def resolve_vp_char(self, char: str) -> KeyBinding | None:
        """Look up a standard 61-key Virtual Piano character (e.g. 't', 'T', '!')."""
        return self._by_char.get(char)

    def all_bindings(self) -> list[KeyBinding]:
        return sorted(self.profile.bindings, key=lambda b: b.midi_note)

    def with_base_midi_note(self, new_base_midi_note: int) -> Keymap:
        """Return a shifted copy of this keymap anchored at a different C2 base note."""
        delta = new_base_midi_note - self.profile.base_midi_note
        if delta == 0:
            return self
        shifted_bindings = [
            b.model_copy(update={"midi_note": b.midi_note + delta})
            for b in self.profile.bindings
            if 0 <= b.midi_note + delta <= 127
        ]
        shifted_profile = self.profile.model_copy(
            update={
                "base_midi_note": new_base_midi_note,
                "min_midi_note": max(0, self.profile.min_midi_note + delta),
                "max_midi_note": min(127, self.profile.max_midi_note + delta),
                "bindings": shifted_bindings,
            }
        )
        return Keymap(shifted_profile)

    @classmethod
    def from_json_file(cls, path: Path) -> Keymap:
        raw = json.loads(path.read_text(encoding="utf-8"))
        profile = KeymapProfile.model_validate(raw)
        return cls(profile)

    def to_json_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.profile.model_dump_json(indent=2), encoding="utf-8")


def build_roblox_61_profile(base_midi_note: int = 36) -> KeymapProfile:
    """Construct the standard 61-key Roblox Virtual Piano profile (C2..C7)."""
    bindings: list[KeyBinding] = []
    for idx, ch in enumerate(VP_61_CHARS):
        midi_note = base_midi_note + idx
        if ch in _SHIFTED_TO_BASE:
            base_key = _SHIFTED_TO_BASE[ch]
            modifier: Modifier = "shift"
        else:
            base_key = ch
            modifier = "none"
        bindings.append(
            KeyBinding(
                midi_note=midi_note,
                key=ch,
                base_key=base_key,
                scancode=KEY_SCANCODES[base_key],
                modifier=modifier,
            )
        )
    return KeymapProfile(
        id="roblox_61",
        name="Roblox 61건반 (표준)",
        description="표준 61건반 가상 피아노 (C2–C7, 흰 건반 1..m + 검은 건반 Shift)",
        key_count=61,
        base_midi_note=base_midi_note,
        min_midi_note=base_midi_note,
        max_midi_note=base_midi_note + 60,
        bindings=bindings,
    )


def build_roblox_88_profile(base_midi_note: int = 36) -> KeymapProfile:
    """Construct the 88-key Roblox Visual Piano profile (A0..C8, Ctrl extensions)."""
    base_61 = build_roblox_61_profile(base_midi_note=base_midi_note)
    bindings: list[KeyBinding] = []

    # Lower 15 keys: A0 (base - 15) .. B1 (base - 1)
    low_start = base_midi_note - len(VP_88_LOW_KEYS)
    for idx, base_key in enumerate(VP_88_LOW_KEYS):
        bindings.append(
            KeyBinding(
                midi_note=low_start + idx,
                key=f"^{base_key}",
                base_key=base_key,
                scancode=KEY_SCANCODES[base_key],
                modifier="ctrl",
            )
        )

    bindings.extend(base_61.bindings)

    # Upper 12 keys: C#7 (base + 61) .. C8 (base + 72)
    high_start = base_midi_note + 61
    for idx, base_key in enumerate(VP_88_HIGH_KEYS):
        bindings.append(
            KeyBinding(
                midi_note=high_start + idx,
                key=f"^{base_key}",
                base_key=base_key,
                scancode=KEY_SCANCODES[base_key],
                modifier="ctrl",
            )
        )

    return KeymapProfile(
        id="roblox_88",
        name="Roblox 88건반 (비주얼 피아노)",
        description="88건반 풀 레인지 (A0–C8, 상하단 확장 음역 Ctrl+키)",
        key_count=88,
        base_midi_note=base_midi_note,
        min_midi_note=low_start,
        max_midi_note=high_start + len(VP_88_HIGH_KEYS) - 1,
        bindings=bindings,
    )
