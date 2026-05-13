#!/usr/bin/env python3

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def normalize_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def element_text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def value_text(element: ET.Element | None) -> str:
    if element is None:
        return "-1"

    tag = normalize_tag(element.tag)
    if tag in {"true", "false"}:
        return tag

    text = element_text(element)
    if text:
        return text

    return "-1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Return the value for a plist/XML key.")
    parser.add_argument("xml_file", type=Path, help="Path to the XML/plist file")
    parser.add_argument("key_name", nargs="?", default="0", help="Key name to look for")
    args = parser.parse_args()

    if not args.xml_file.exists():
        print("-1")
        return 0

    try:
        root = ET.parse(args.xml_file).getroot()
    except ET.ParseError:
        print("-1")
        return 0

    for parent in root.iter():
        children = list(parent)
        for index, child in enumerate(children):
            if normalize_tag(child.tag) != "key":
                continue
            if element_text(child) != args.key_name:
                continue

            sibling = children[index + 1] if index + 1 < len(children) else None
            print(value_text(sibling))
            return 0

    print("-1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
