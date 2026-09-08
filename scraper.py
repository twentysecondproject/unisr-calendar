import hashlib
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://orario.unisr.it/default.asp"

COURSE_ID = "10321"
COURSE_YEAR = "1"

OUTPUT_FILE = Path("unirsr.ics")

# How far backwards/forwards we scan.
DAYS_BACK = 7
DAYS_FORWARD = 180

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 Safari/605.1.15"
    )
})


def build_url(day: date) -> str:
    params = {
        "ext": "ok",
        "data": day.strftime("%d/%m/%Y"),
        "CDS_ID": COURSE_ID,
        "DAORA": "0",
        "ANNO_CORSO": COURSE_YEAR,
        "id_palazzo": "0",
        "id_aula": "0",
        "lezioni": "ok",
        "esami": "ok",
    }

    return f"{BASE_URL}?{urlencode(params)}"


def clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    return value.strip()


def parse_time_range(text: str):
    """
    Extracts a start/end time from text such as:
    09:30 - 11:30
    09:30–11:30
    09.30 - 11.30
    """
    text = text.replace(".", ":").replace("–", "-").replace("—", "-")

    match = re.search(
        r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})",
        text
    )

    if not match:
        return None

    return match.group(1), match.group(2)


def extract_rows(html: str, day: date):
    soup = BeautifulSoup(html, "html.parser")
    lessons = []

    current_location = ""

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):

            # ---------------------------------------------------------
            # 1. Check whether this row contains a classroom.
            #
            # UniSR may print the classroom once above several lessons.
            # Therefore we keep the last classroom encountered and
            # associate it with following lesson rows.
            # ---------------------------------------------------------

            row_text = clean_text(
                tr.get_text(" ", strip=True)
            )

            location_match = re.search(
                r"([A-ZÀ-ÖØ-Ý0-9 .'-]+)\s*-\s*Aula\s+"
                r"([A-Z]{1,5}\d{2,4})"
                r"(?:\s*\(([^)]*)\))?",
                row_text,
                flags=re.IGNORECASE,
            )

            if location_match:
                building = clean_text(location_match.group(1))
                room = clean_text(location_match.group(2))
                floor = clean_text(location_match.group(3) or "")

                current_location = (
                    f"{building} Aula {room}"
                )

                if floor:
                    current_location += f" ({floor})"

            # ---------------------------------------------------------
            # 2. Extract the cells of the row.
            # ---------------------------------------------------------

            cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["td", "th"])
            ]

            cells = [cell for cell in cells if cell]

            if not cells:
                continue

            combined = " | ".join(cells)

            # ---------------------------------------------------------
            # 3. Only keep rows containing a lesson title.
            # ---------------------------------------------------------

            if "Lezione:" not in combined:
                continue

            # ---------------------------------------------------------
            # 4. Find the time range.
            # ---------------------------------------------------------

            time_match = re.search(
                r"(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})",
                combined,
            )

            if not time_match:
                continue

            start_time = time_match.group(1)
            end_time = time_match.group(2)

            # ---------------------------------------------------------
            # 5. Store the classroom together with the lesson.
            # ---------------------------------------------------------

            lessons.append({
                "date": day,
                "start": start_time,
                "end": end_time,
                "raw": combined,
                "cells": cells,
                "location": current_location,
            })

    # Remove exact duplicates.
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
    """
    Keep the UniSR lesson title exactly as displayed on the timetable.

    The classroom is extracted by extract_rows(), because UniSR may
    display the classroom separately from the lesson row.
    """

    cells = lesson["cells"]

    combined = " | ".join(cells)

    # Find the complete UniSR lesson title.
    match = re.search(
        r"(Lezione:\s*.*?\(docente:\s*.*?\))",
        combined,
        flags=re.IGNORECASE,
    )

    if match:
        subject = clean_text(match.group(1))
    else:
        # Fallback: look for any cell beginning with "Lezione:"
        subject = ""

        for cell in cells:
            if cell.lower().startswith("lezione:"):
                subject = clean_text(cell)
                break

        if not subject:
            subject = "UniSR Lesson"

    # The classroom is extracted by extract_rows().
    location = clean_text(
        lesson.get("location", "")
    )

    return {
        "date": lesson["date"],
        "start": lesson["start"],
        "end": lesson["end"],
        "subject": subject,
        "teacher": "",
        "location": location,
        "raw": lesson["raw"],
    }


def make_uid(lesson):
    """
    Stable UID.

    We intentionally use subject + date + occurrence information rather
    than the room/teacher so that changing a room or teacher updates the
    existing Apple Calendar event instead of creating a duplicate.
    """

    base = (
        f"{lesson['date'].isoformat()}|"
        f"{lesson['subject'].lower()}"
    )

    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]

    return f"{digest}@unirsr-calendar"


def escape_ics(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ics_line(line, limit=73):
    """
    RFC-style line folding for iCalendar.
    """
    result = []

    while len(line) > limit:
        result.append(line[:limit])
        line = " " + line[limit:]

    result.append(line)

    return "\r\n".join(result)


def format_datetime(day, time_string):
    return f"{day.strftime('%Y%m%d')}T{time_string.replace(':', '')}00"


def generate_ics(lessons):
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//twentysecondproject//UniSR Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:UniSR International MD",
        "X-WR-TIMEZONE:Europe/Rome",
    ]

    for lesson in lessons:
        uid = make_uid(lesson)

        start = format_datetime(
            lesson["date"],
            lesson["start"],
        )

        end = format_datetime(
            lesson["date"],
            lesson["end"],
        )

        summary = escape_ics(lesson["subject"])

        description = escape_ics(
            f"Docente: {lesson['teacher']}\n"
            f"UniSR International Medical Doctor Program\n"
            f"Raw timetable: {lesson['raw']}"
        )

        location = escape_ics(lesson["location"])

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=Europe/Rome:{start}",
            f"DTEND;TZID=Europe/Rome:{end}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            f"LOCATION:{location}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    return "\r\n".join(
        fold_ics_line(line)
        for line in lines
    ) + "\r\n"


def main():
    today = date.today()

    start_date = today - timedelta(days=DAYS_BACK)
    end_date = today + timedelta(days=DAYS_FORWARD)

    print(
        f"Scanning UniSR timetable from "
        f"{start_date} to {end_date}"
    )

    all_lessons = []

    current = start_date

    while current <= end_date:
        url = build_url(current)

        print(
            f"Fetching {current.strftime('%d/%m/%Y')}..."
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
                f"  → {len(rows)} timetable rows"
            )

        except Exception as exc:
            print(
                f"  ERROR: {current}: {exc}"
            )

        current += timedelta(days=1)

        # Be polite to the UniSR server.
        time.sleep(0.15)

    # Remove duplicates.
    unique = {}

    for lesson in all_lessons:
        key = (
            lesson["date"],
            lesson["start"],
            lesson["end"],
            lesson["subject"],
            lesson["location"],
        )

        unique[key] = lesson

    lessons = sorted(
        unique.values(),
        key=lambda x: (
            x["date"],
            x["start"],
            x["subject"],
        ),
    )

    print(f"\nTotal lessons: {len(lessons)}")

    ics = generate_ics(lessons)

    OUTPUT_FILE.write_text(
        ics,
        encoding="utf-8",
    )

    print(f"Written: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
