import requests
import json
import os
import sys
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Environment variables (from GitHub Secrets)
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DB_ID = os.getenv('NOTION_DB_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS')
CALENDAR_ID = os.getenv('CALENDAR_ID', 'primary')
# Configurable property names (optional, defaults to 'Name' and 'Date' for backward compatibility)
NOTION_TITLE_PROPERTY = os.getenv('NOTION_TITLE_PROPERTY', 'Name')
NOTION_DATE_PROPERTY = os.getenv('NOTION_DATE_PROPERTY', 'Date')

# Titles that must NEVER be written back into Notion (data-loss guard)
BLANK_TITLE_SENTINELS = {'', 'untitled event', 'untitled'}


def title_is_blank(title):
    """A title is 'blank' if it is empty, whitespace, or a placeholder like
    'Untitled Event'. Such titles must never overwrite a real Notion title."""
    if title is None:
        return True
    return title.strip().lower() in BLANK_TITLE_SENTINELS


def parse_iso_datetime(value):
    """Parse ISO/RFC3339 timestamps from Notion/Google into aware datetimes."""
    if not value:
        return None
    try:
        if isinstance(value, str) and value.endswith('Z'):
            value = value[:-1] + '+00:00'
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _norm_date_value(value):
    """Normalize a date/datetime string for comparison.
    Returns ('date', 'YYYY-MM-DD') for all-day, ('datetime', aware_dt) for timed,
    ('raw', value) if unparseable, or (None, None) if empty."""
    if not value:
        return (None, None)
    if isinstance(value, str) and len(value) == 10:
        return ('date', value)
    dt = parse_iso_datetime(value)
    if dt is None:
        return ('raw', value)
    return ('datetime', dt)


def date_values_equal(a, b):
    """Compare two date/datetime values as instants (ignores sub-second and
    timezone-format differences) so we don't detect fake changes every run."""
    ta, va = _norm_date_value(a)
    tb, vb = _norm_date_value(b)
    if ta != tb:
        return False
    if ta == 'datetime':
        try:
            return abs((va - vb).total_seconds()) < 1
        except Exception:
            return va == vb
    return va == vb


def validate_env():
    """Validate required environment variables are present and non-empty."""
    missing = []
    if not NOTION_TOKEN:
        missing.append('NOTION_TOKEN')
    if not NOTION_DB_ID:
        missing.append('NOTION_DB_ID')
    if not GOOGLE_CREDENTIALS_JSON:
        missing.append('GOOGLE_CREDENTIALS')
    if not CALENDAR_ID:
        missing.append('CALENDAR_ID')

    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        print("Ensure GitHub Secrets are configured for these names.")
        sys.exit(1)


def get_google_calendar_service():
    """Initialize the Google Calendar API service"""
    try:
        credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    except Exception as e:
        raise RuntimeError(f"Failed to parse GOOGLE_CREDENTIALS JSON: {e}")

    try:
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=credentials)
    except Exception as e:
        raise RuntimeError(f"Failed to initialize Google Calendar client: {e}")


def get_notion_items():
    """Fetch all items from the Notion database (handles pagination)"""
    headers = {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

    all_items = []
    next_cursor = None
    page_count = 0

    while True:
        page_count += 1
        request_body = {}
        if next_cursor:
            request_body['start_cursor'] = next_cursor

        response = requests.post(
            f'https://api.notion.com/v1/databases/{NOTION_DB_ID}/query',
            headers=headers,
            json=request_body
        )

        if response.status_code != 200:
            print(f"❌ Error fetching Notion data: {response.status_code}")
            print(response.text)
            if page_count == 1:
                return []
            break

        data = response.json()
        page_items = data.get('results', [])
        all_items.extend(page_items)

        has_more = data.get('has_more', False)
        next_cursor = data.get('next_cursor')

        if not has_more or not next_cursor:
            break

        print(f"📄 Fetched page {page_count} ({len(page_items)} items)...")

    if page_count > 1:
        print(f"📚 Pagination complete: fetched {page_count} pages, {len(all_items)} total items")

    return all_items


def update_notion_page(page_id, title, start_date, end_date=None):
    """Update a Notion page's date, and its title ONLY if a real (non-blank)
    title is given. This is the core data-loss guard: a blank/'Untitled' title
    is never written back into Notion."""
    headers = {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

    date_property = {'start': start_date}
    if end_date and end_date != start_date:
        date_property['end'] = end_date

    properties = {
        NOTION_DATE_PROPERTY: {
            'date': date_property
        }
    }

    # GUARD: only write the title when it is real and non-blank.
    if not title_is_blank(title):
        properties[NOTION_TITLE_PROPERTY] = {
            'title': [{'text': {'content': title}}]
        }

    data = {'properties': properties}

    response = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=headers,
        json=data
    )
    return response.status_code == 200


def create_notion_page(title, start_date, end_date=None, gcal_event_id=None):
    """Create a new Notion page"""
    headers = {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

    date_property = {'start': start_date}
    if end_date and end_date != start_date:
        date_property['end'] = end_date

    data = {
        'parent': {'database_id': NOTION_DB_ID},
        'properties': {
            NOTION_TITLE_PROPERTY: {
                'title': [{'text': {'content': title}}]
            },
            NOTION_DATE_PROPERTY: {
                'date': date_property
            }
        }
    }

    response = requests.post(
        'https://api.notion.com/v1/pages',
        headers=headers,
        json=data
    )

    if response.status_code == 200:
        return response.json()['id']
    return None


def delete_notion_page(page_id):
    """Delete (archive) a Notion page"""
    headers = {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

    data = {'archived': True}
    response = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=headers,
        json=data
    )
    return response.status_code == 200


def gcal_event_to_notion_date(gcal_event):
    """Convert Google Calendar event to Notion date format"""
    start = gcal_event.get('start', {})
    end = gcal_event.get('end', {})

    # All-day event
    if 'date' in start:
        start_date = start['date']
        end_date = end.get('date')
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)
            end_date = end_dt.strftime("%Y-%m-%d")
            if end_date == start_date:
                end_date = None
        return start_date, end_date

    # Timed event
    elif 'dateTime' in start:
        start_datetime = start['dateTime']
        end_datetime = end.get('dateTime')
        return start_datetime, end_datetime

    return None, None


def notion_item_to_date(notion_item):
    """Extract date values from a Notion item"""
    properties = notion_item.get('properties', {})

    if NOTION_DATE_PROPERTY in properties:
        date_prop = properties[NOTION_DATE_PROPERTY]
        if date_prop['type'] == 'date' and date_prop['date']:
            start_date = date_prop['date']['start']
            end_date = date_prop['date'].get('end')
            return start_date, end_date

    return None, None


def extract_notion_title(notion_item):
    """Extract full title from a Notion item, concatenating all title segments"""
    properties = notion_item.get('properties', {})

    if NOTION_TITLE_PROPERTY in properties:
        title_prop = properties[NOTION_TITLE_PROPERTY]
        if title_prop['type'] == 'title' and title_prop['title']:
            title_parts = [segment.get('plain_text', '') for segment in title_prop['title']]
            return ''.join(title_parts)

    return "Untitled Event"


def notion_to_calendar_event(notion_item):
    """Convert a Notion item to a Google Calendar event"""
    properties = notion_item.get('properties', {})

    title = extract_notion_title(notion_item)

    start_time = None
    end_time = None
    is_all_day = False

    if NOTION_DATE_PROPERTY in properties:
        date_prop = properties[NOTION_DATE_PROPERTY]
        if date_prop['type'] == 'date' and date_prop['date']:
            start_time = date_prop['date']['start']
            end_time = date_prop['date'].get('end')

            if len(start_time) == 10:
                is_all_day = True
                if not end_time:
                    end_date = datetime.strptime(start_time, "%Y-%m-%d") + timedelta(days=1)
                    end_time = end_date.strftime("%Y-%m-%d")
                else:
                    end_date = datetime.strptime(end_time, "%Y-%m-%d") + timedelta(days=1)
                    end_time = end_date.strftime("%Y-%m-%d")

    if not start_time:
        return None

    event = {
        'summary': title,
        'description': f"Synced from Notion: {notion_item['url']}",
    }

    if is_all_day:
        event['start'] = {'date': start_time}
        event['end'] = {'date': end_time}
    else:
        if not end_time:
            end_time = start_time
        event['start'] = {'dateTime': start_time}
        event['end'] = {'dateTime': end_time}

    return event


def _events_are_equivalent(new_event, existing_event):
    """True if the calendar event already matches what Notion would set
    (summary + start/end), so we can skip a needless update."""
    if new_event.get('summary', '') != existing_event.get('summary', ''):
        return False
    new_start, new_end = gcal_event_to_notion_date(new_event)
    ex_start, ex_end = gcal_event_to_notion_date(existing_event)
    return date_values_equal(new_start, ex_start) and date_values_equal(new_end, ex_end)


def sync_notion_to_calendar(service, notion_items, notion_ids):
    """Sync Notion → Google Calendar"""
    print("🔄 Syncing Notion → Google Calendar...")

    created_count = 0
    updated_count = 0
    skipped_count = 0
    deleted_count = 0

    for item in notion_items:
        try:
            event = notion_to_calendar_event(item)
            if not event:
                print("⏭️ Skipping item without valid date")
                skipped_count += 1
                continue

            notion_id = item['id']
            event['extendedProperties'] = {'private': {'notion_id': notion_id}}

            existing = service.events().list(
                calendarId=CALENDAR_ID,
                privateExtendedProperty=f"notion_id={notion_id}"
            ).execute().get('items', [])

            if existing:
                existing_event = existing[0]
                existing_event_id = existing_event['id']

                # Skip if the calendar already matches Notion (prevents churn)
                if _events_are_equivalent(event, existing_event):
                    continue

                notion_last_edited = parse_iso_datetime(item.get('last_edited_time'))
                gcal_last_updated = parse_iso_datetime(existing_event.get('updated'))

                if notion_last_edited and gcal_last_updated and notion_last_edited <= gcal_last_updated:
                    print(
                        "⏭️ Skipping Notion → Calendar update "
                        f"(calendar newer or same) for: {event['summary']}"
                    )
                    continue

                service.events().update(
                    calendarId=CALENDAR_ID,
                    eventId=existing_event_id,
                    body=event
                ).execute()
                print(f"🔄 Updated calendar event: {event['summary']}")
                updated_count += 1
            else:
                service.events().insert(
                    calendarId=CALENDAR_ID,
                    body=event
                ).execute()
                print(f"✅ Created calendar event: {event['summary']}")
                created_count += 1

        except Exception as e:
            print(f"❌ Error syncing item to calendar: {e}")
            continue

    # --- DELETE EVENTS NO LONGER IN NOTION ---
    try:
        print("🔍 Checking for calendar events to delete...")

        gcal_events = service.events().list(
            calendarId=CALENDAR_ID,
            maxResults=2500
        ).execute().get('items', [])

        synced_events = []
        for event in gcal_events:
            extended_props = event.get('extendedProperties', {}).get('private', {})
            if 'notion_id' in extended_props:
                synced_events.append(event)

        print(f"🔍 Found {len(synced_events)} previously synced events")

        for g_event in synced_events:
            notion_id = g_event['extendedProperties']['private']['notion_id']
            if notion_id not in notion_ids:
                service.events().delete(
                    calendarId=CALENDAR_ID,
                    eventId=g_event['id']
                ).execute()
                print(f"🗑️ Deleted calendar event: {g_event.get('summary', 'Untitled')}")
                deleted_count += 1

    except Exception as e:
        print(f"❌ Error during calendar deletion sync: {e}")

    return created_count, updated_count, skipped_count, deleted_count


def sync_calendar_to_notion(service, notion_items):
    """Sync Google Calendar → Notion"""
    print("🔄 Syncing Google Calendar → Notion...")

    created_count = 0
    updated_count = 0
    deleted_count = 0

    notion_map = {item['id']: item for item in notion_items}

    try:
        gcal_events = service.events().list(
            calendarId=CALENDAR_ID,
            maxResults=2500
        ).execute().get('items', [])

        for gcal_event in gcal_events:
            extended_props = gcal_event.get('extendedProperties', {}).get('private', {})
            notion_id = extended_props.get('notion_id')

            if not notion_id:
                # New event created directly in Google Calendar.
                title = gcal_event.get('summary', '')
                start_date, end_date = gcal_event_to_notion_date(gcal_event)

                # GUARD: don't turn blank / 'Untitled Event' junk into Notion tasks.
                if title_is_blank(title):
                    print("⏭️ Skipping blank/untitled calendar event (not creating a Notion task)")
                    continue

                if start_date:
                    new_notion_id = create_notion_page(title, start_date, end_date)
                    if new_notion_id:
                        gcal_event['extendedProperties'] = {
                            'private': {'notion_id': new_notion_id}
                        }
                        service.events().update(
                            calendarId=CALENDAR_ID,
                            eventId=gcal_event['id'],
                            body=gcal_event
                        ).execute()
                        print(f"✅ Created Notion page from calendar event: {title}")
                        created_count += 1
                continue

            # If the Notion page is gone, remove the orphaned calendar event.
            if notion_id not in notion_map:
                service.events().delete(
                    calendarId=CALENDAR_ID,
                    eventId=gcal_event['id']
                ).execute()
                print(f"🗑️ Deleted calendar event (Notion page gone): {gcal_event.get('summary')}")
                continue

            notion_item = notion_map[notion_id]

            notion_last_edited = parse_iso_datetime(notion_item.get('last_edited_time'))
            gcal_last_updated = parse_iso_datetime(gcal_event.get('updated'))

            # If Notion is newer or same, do NOT overwrite it from Calendar.
            if notion_last_edited and gcal_last_updated and notion_last_edited >= gcal_last_updated:
                continue

            notion_title = extract_notion_title(notion_item)
            notion_start, notion_end = notion_item_to_date(notion_item)

            gcal_title = gcal_event.get('summary', '')
            gcal_start, gcal_end = gcal_event_to_notion_date(gcal_event)

            changes = []

            # TITLE: only a real, non-blank, genuinely different title counts.
            title_changed = (not title_is_blank(gcal_title)) and (gcal_title != notion_title)
            if title_changed:
                changes.append(f"title: '{notion_title}' → '{gcal_title}'")

            # DATES: compare as instants to avoid fake changes.
            start_changed = not date_values_equal(gcal_start, notion_start)
            if start_changed:
                changes.append(f"start: '{notion_start or '(none)'}' → '{gcal_start or '(none)'}'")

            end_changed = not date_values_equal(gcal_end, notion_end)
            if end_changed:
                changes.append(f"end: '{notion_end or '(none)'}' → '{gcal_end or '(none)'}'")

            if changes and gcal_start:
                print(f"📝 Changes detected: {', '.join(changes)}")
                # Pass the title ONLY if it really changed; otherwise None so the
                # existing Notion title is preserved (update_notion_page also guards).
                title_to_write = gcal_title if title_changed else None
                if update_notion_page(notion_id, title_to_write, gcal_start, gcal_end):
                    print(f"🔄 Updated Notion page: {gcal_title if title_changed else notion_title}")
                    updated_count += 1

    except Exception as e:
        print(f"❌ Error during calendar to Notion sync: {e}")

    return created_count, updated_count, deleted_count


def main():
    """Main sync function - handles both directions"""
    print("🔄 Starting 2-Way Notion ↔ Google Calendar sync...")
    print(f"📝 Using property names: Title='{NOTION_TITLE_PROPERTY}', Date='{NOTION_DATE_PROPERTY}'")

    validate_env()

    try:
        service = get_google_calendar_service()
        print("🔗 Connected to Google Calendar")
    except Exception as e:
        print(f"❌ Failed to connect to Google Calendar: {e}")
        return

    # Calendar → Notion first, so real manual calendar edits win over older Notion values.
    notion_items = get_notion_items()
    print(f"📋 Found {len(notion_items)} Notion items")
    c2n_created, c2n_updated, c2n_deleted = sync_calendar_to_notion(
        service, notion_items
    )

    # Re-fetch so Notion → Calendar uses the latest values.
    notion_items = get_notion_items()
    print(f"📋 Found {len(notion_items)} Notion items after Calendar → Notion sync")
    notion_ids = set(item['id'] for item in notion_items)

    n2c_created, n2c_updated, n2c_skipped, n2c_deleted = sync_notion_to_calendar(
        service, notion_items, notion_ids
    )

    print(f"""
🎉 2-Way Sync Complete!

Notion → Calendar:
  Created: {n2c_created}
  Updated: {n2c_updated}
  Skipped: {n2c_skipped}
  Deleted: {n2c_deleted}

Calendar → Notion:
  Created: {c2n_created}
  Updated: {c2n_updated}
  Deleted: {c2n_deleted}
""")


if __name__ == "__main__":
    main()
