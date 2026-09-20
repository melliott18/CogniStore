"""Reserved storage components under supported filesystem name comparisons."""

from __future__ import annotations


class InvalidStorageNamespaceError(ValueError):
    """A caller-supplied coordinate would cross a reserved storage boundary."""


# Unicode 17.0 Default_Ignorable_Code_Point, coalesced inclusive ranges.
# Casefolded ext4 can ignore these characters; this also includes all 16 HFS+
# ignorables. Pin the table so supported Python Unicode versions agree.
# https://www.unicode.org/Public/17.0.0/ucd/DerivedCoreProperties.txt
_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_IGNORABLE_TRANSLATION = dict.fromkeys(
    codepoint for first, last in _IGNORABLE_RANGES for codepoint in range(first, last + 1)
)


def storage_component_identity(component: str) -> str:
    """Conservatively compare namespace components, never rewrite object names."""
    return component.translate(_IGNORABLE_TRANSLATION).casefold()


def is_reserved_component(component: str, reserved: str) -> bool:
    """Match a fixed lowercase ASCII reservation without rewriting object names.

    Casefold also catches Unicode case aliases such as long s (U+017F).
    Canonical Unicode normalization adds no aliases to these ASCII reservations.
    """
    return storage_component_identity(component) == reserved
