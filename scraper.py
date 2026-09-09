import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://orario.unisr.it/default.asp"

DAYS_BACK = 7
DAYS_FORWARD = 180

OUTPUTS = {
    "international-md": {
        "course_id": "10321",
        "course_year": "1",
        "line": None,
        "filename": "calendario-unisr.ics",
    },

    "italiano-s1-azzurro": {
        "course_id": "10311",
        "course_year": "1",
        "line": "AZZURRA",
        "filename": "italiano-sezione1-azzurro.ics",
    },
    "italiano-s1-bianco": {
        "course_id": "10311",
        "course_year": "1",
        "line": "BIANCA",
        "filename": "italiano-sezione1-bianco.ics",
    },

    "italiano-s2-giallo": {
        "course_id": "10325",
        "course_year": "1",
        "line": "GIALLA",
        "filename": "italiano-sezione2-giallo.ics",
    },
    "italiano-s2-verde": {
        "course_id": "10325",
        "course_year": "1",
        "line": "VERDE",
        "filename": "italiano-sezione2-verde.ics",
    },

    "italiano-s3-rosso": {
        "course_id": "10324",
        "course_year": "1",
        "line": "ROSSA",
        "filename": "italiano-sezione3-rosso.ics",
    },
    "italiano-s3-viola": {
        "course_id": "10324",
        "course_year": "1",
        "line": "VIOLA",
        "filename": "italiano-sezione3-viola.ics",
    },
}


STATE_DIR = Path("states")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 Safari/605.1.15"
    )
})


def build_url(day: date, course_id: str, course_year: str) -> str:
    params = {
        "ext": "ok",
        "data": day.strftime("%d/%m/%Y"),
        "CDS_ID": course_id,
        "DAORA": "0",
        "ANNO_CORSO": course_year,
        "id_palazzo": "0",
        "id_aula": "0",
        "lezioni": "ok",
    }

    return f"{BASE_URL}?{urlencode(params)}"


def clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    return value.strip()


def extract_rows(html: str, day: date):
    soup = BeautifulSoup(html, "html.parser")

    lessons = []
    current_location = ""

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):

            row_text = clean_text(
                tr.get_text(" ", strip=True)
            )

            # Keep the existing room-detection system.
            location_match = re.search(
                r"([A-ZÀ-ÖØ-Ý0-9 .'-]+)\s*-\s*Aula\s+"
                r"([A-Z]{1,5}\d{2,4})"
                r"(?:\s*\(([^)]*)\))?",
                row_text,
                flags=re.IGNORECASE,
            )

            if location_match:
                building = clean_text(
                    location_match.group(1)
                )

                room = clean_text(
                    location_match.group(2)
                )

                floor = clean_text(
                    location_match.group(3) or ""
                )

                current_location = (
                    f"{building} Aula {room}"
                )

                if floor:
                    current_location += (
                        f" ({floor})"
                    )

            cells = [
                clean_text(
                    cell.get_text(" ", strip=True)
                )
                for cell in tr.find_all(["td", "th"])
            ]

            cells = [
                cell for cell in cells
                if cell
            ]

            if not cells:
                continue

            combined = " | ".join(cells)

            if "Lezione:" not in combined:
                continue

            time_match = re.search(
                r"(\d{1,2}:\d{2})\s*[-–—]\s*"
                r"(\d{1,2}:\d{2})",
                combined,
            )

            if not time_match:
                continue

            lessons.append({
                "date": day,
                "start": time_match.group(1),
                "end": time_match.group(2),
                "raw": combined,
                "cells": cells,
                "location": current_location,
            })

    unique = []
    seen = set()

    for lesson in lessons:
        key = (
            lesson["date"],
            lesson["start"],
            lesson["end"],
            lesson["raw"],
            lesson["location"],
        )

        if key not in seen:
            seen.add(key)
            unique.append(lesson)

    return unique


def parse_lesson(lesson):
    cells = lesson["cells"]
    combined = " | ".join(cells)

    match = re.search(
        r"(Lezione:\s*.*?\(docente:\s*.*?\))",
        combined,
        flags=re.IGNORECASE,
    )

    if match:
        subject = clean_text(match.group(1))
    else:
        subject = ""

        for cell in cells:
            if cell.lower().startswith("lezione:"):
                subject = clean_text(cell)
                break

        if not subject:
            subject = "UniSR Lesson"

    return {
        "date": lesson["date"],
        "start": lesson["start"],
        "end": lesson["end"],
        "subject": subject,
        "location": clean_text(
            lesson.get("location", "")
        ),
        "raw": lesson["raw"],
    }


def extract_line(subject):
    match = re.search(
        r"\(LINEA\s+([A-ZÀ-ÖØ-Ý]+)\)",
        subject,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1).upper()


def filter_lessons(lessons, line):
    if line is None:
        return lessons

    filtered = []

    for lesson in lessons:
        lesson_line = extract_line(
            lesson["subject"]
        )

        if lesson_line == line:
            filtered.append(lesson)

    return filtered


def lesson_identity(lesson):
    return (
        lesson["date"].isoformat(),
        lesson["subject"].lower(),
    )


def make_uid(calendar_key, lesson, occurrence):
    base = (
        f"{calendar_key}|"
        f"{lesson['date'].isoformat()}|"
        f"{lesson['subject'].lower()}|"
        f"{occurrence}"
    )

    digest = hashlib.sha256(
        base.encode("utf-8")
    ).hexdigest()[:24]

    return f"{digest}@unirsr-calendar"


def assign_uids(lessons, calendar_key):
    grouped = {}

    for lesson in lessons:
        key = lesson_identity(lesson)
        grouped.setdefault(key, []).append(lesson)

    for group in grouped.values():

        group.sort(
            key=lambda x: (
                x["start"],
                x["end"],
                x["location"],
                x["raw"],
            )
        )

        for occurrence, lesson in enumerate(
            group,
            start=1,
        ):
            lesson["uid"] = make_uid(
                calendar_key,
                lesson,
                occurrence,
            )

    return lessons


def state_file(calendar_key):
    return STATE_DIR / f"{calendar_key}.json"


def load_state(calendar_key):
    path = state_file(calendar_key)

    if not path.exists():
        return {}

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        print(
            f"WARNING: Could not read state for "
            f"{calendar_key}. Starting fresh."
        )

        return {}


def save_state(calendar_key, state):
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    state_file(calendar_key).write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def escape_ics(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ics_line(line, limit=73):
    result = []

    while len(line) > limit:
        result.append(line[:limit])
        line = " " + line[limit:]

    result.append(line)

    return "\r\n".join(result)


def format_datetime(day, time_string):
    return (
        f"{day.strftime('%Y%m%d')}"
        f"T{time_string.replace(':', '')}00"
    )


def generate_ics(lessons, cancelled):
    now = datetime.utcnow().strftime(
        "%Y%m%dT%H%M%SZ"
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//twentysecondproject//UniSR Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:UniSR Calendar",
        "X-WR-CALDESC:UniSR timetable for calendars created by Filippo Genoni",
        "X-WR-TIMEZONE:Europe/Rome",
    ]

    for lesson in lessons:

        start = format_datetime(
            lesson["date"],
            lesson["start"],
        )

        end = format_datetime(
            lesson["date"],
            lesson["end"],
        )

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{lesson['uid']}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=Europe/Rome:{start}",
            f"DTEND;TZID=Europe/Rome:{end}",
            f"SUMMARY:{escape_ics(lesson['subject'])}",
            f"LOCATION:{escape_ics(lesson['location'])}",
            "DESCRIPTION:Qualche problema/any issues? Scrivimi/Please contact me on Instagram @fil_genna\\nhttps://twentysecondproject.github.io/unirsr-calendar/",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])

    for event in cancelled:

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{event['uid']}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=Europe/Rome:{event['start']}",
            f"DTEND;TZID=Europe/Rome:{event['end']}",
            f"SUMMARY:{escape_ics(event['subject'])}",
            f"LOCATION:{escape_ics(event.get('location', ''))}",
            "STATUS:CANCELLED",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    return "\r\n".join(
        fold_ics_line(line)
        for line in lines
    ) + "\r\n"


def process_calendar(
    calendar_key,
    config,
    all_lessons,
):
    lessons = filter_lessons(
        all_lessons,
        config["line"],
    )

    lessons = assign_uids(
        lessons,
        calendar_key,
    )

    unique = {}

    for lesson in lessons:

        key = (
            lesson["date"],
            lesson["start"],
            lesson["end"],
            lesson["subject"],
            lesson["location"],
        )

        unique[key] = lesson

    lessons = list(unique.values())

    lessons.sort(
        key=lambda x: (
            x["date"],
            x["start"],
            x["subject"],
        )
    )

    old_state = load_state(
        calendar_key
    )

    current_state = {
        lesson["uid"]: {
            "uid": lesson["uid"],
            "date": lesson["date"].isoformat(),
            "start": format_datetime(
                lesson["date"],
                lesson["start"],
            ),
            "end": format_datetime(
                lesson["date"],
                lesson["end"],
            ),
            "subject": lesson["subject"],
            "location": lesson["location"],
        }
        for lesson in lessons
    }

    cancelled = []

    for uid, old_event in old_state.items():

        if uid not in current_state:

            print(
                f"  CANCELLED [{calendar_key}]: "
                f"{old_event.get('date')} "
                f"{old_event.get('subject')}"
            )

            cancelled.append(old_event)

    ics = generate_ics(
        lessons,
        cancelled,
    )

    output_file = Path(
        config["filename"]
    )

    output_file.write_text(
        ics,
        encoding="utf-8",
    )

    save_state(
        calendar_key,
        current_state,
    )

    print(
        f"  {calendar_key}: "
        f"{len(lessons)} active, "
        f"{len(cancelled)} cancelled"
    )


def main():
    today = date.today()

    start_date = (
        today - timedelta(days=DAYS_BACK)
    )

    end_date = (
        today + timedelta(days=DAYS_FORWARD)
    )

    print(
        f"Scanning UniSR timetables from "
        f"{start_date} to {end_date}"
    )

    fetched_courses = {}

    # Fetch each unique CDS only once.
    for config in OUTPUTS.values():

        course_id = config["course_id"]
        course_year = config["course_year"]

        course_key = (
            course_id,
            course_year,
        )

        if course_key in fetched_courses:
            continue

        print(
            f"\nFetching course {course_id}..."
        )

        all_lessons = []

        current = start_date

        while current <= end_date:

            url = build_url(
                current,
                course_id,
                course_year,
            )

            print(
                f"  Fetching "
                f"{current.strftime('%d/%m/%Y')}..."
            )

            try:
                response = SESSION.get(
                    url,
                    timeout=30,
                )

                response.raise_for_status()

                rows = extract_rows(
                    response.text,
                    current,
                )

                for row in rows:
                    all_lessons.append(
                        parse_lesson(row)
                    )

                print(
                    f"    → {len(rows)} rows"
                )

            except Exception as exc:

                print(
                    f"    ERROR: "
                    f"{current}: {exc}"
                )

            current += timedelta(days=1)

            time.sleep(0.15)

        fetched_courses[course_key] = (
            all_lessons
        )

    print("\nGenerating calendars...")

    for calendar_key, config in OUTPUTS.items():

        course_key = (
            config["course_id"],
            config["course_year"],
        )

        all_lessons = fetched_courses[
            course_key
        ]

        process_calendar(
            calendar_key,
            config,
            all_lessons,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
