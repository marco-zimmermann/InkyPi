"""UI-only syllabus cleanup, using synthetic bookings exclusively."""
from dataclasses import replace

import pytest
from PIL import ImageDraw
from plugins.flight_duties.rendering import display_activity, render_dashboard
from tests.test_flight_duties import NOW, RAW, report


@pytest.mark.parametrize('title,expected', [
    ('2025/10 Training flight, circuits', 'Training flight, circuits'),
    ('E.1.11 Training flight, circuits', 'Training flight, circuits'),
    ('2025/10 E.1.11 Training flight, circuits', 'Training flight, circuits'),
    ('2025/10 - E.1.11 - Training flight, circuits', 'Training flight, circuits'),
    ('2025/10 – E.1.11 – Training flight, circuits', 'Training flight, circuits'),
    ('2025/10: E.1.11: Training flight, circuits', 'Training flight, circuits'),
    ('  2025/10  –  E.1.11 :  Training flight, circuits  ', 'Training flight, circuits'),
    ('Training flight, circuits', 'Training flight, circuits'),
    ('Review 2025/10 E.1.11 circuits', 'Review 2025/10 E.1.11 circuits'),
    ('2025/10 Review E.1.11 circuits', 'Review E.1.11 circuits'),
    ('E.1.11suffix circuits', 'E.1.11suffix circuits'),
    ('2025/100 Training flight', '2025/100 Training flight'),
    ('2025/11a Training flight', 'Training flight'),
    ('2025/11a 🅃 Synthetic syllabus - E.1.11 Training flight, circuits', 'Training flight, circuits'),
    ('2025/10 🅃 Synthetic course – E.3.8 – Navigation exercise', 'Navigation exercise'),
    ('2025/11b 🅃 Longer synthetic course name: E.1.10: Training flight', 'Training flight'),
    ('Review 2025/11a 🅃 Synthetic syllabus - E.1.11 circuits', 'Review 2025/11a 🅃 Synthetic syllabus - E.1.11 circuits'),
    ('Training flight - E.1.11 review', 'Training flight - E.1.11 review'),
])
def test_display_activity_prefixes(title, expected):
    assert display_activity(title) == expected
    assert display_activity(expected) == expected


def test_featured_and_list_cleaned_without_changing_source(monkeypatch):
    result = report()
    schedule = result['schedule']
    prefixed = '2025/11a 🅃 Synthetic syllabus - E.1.11 Training flight, circuits'
    duties = tuple(replace(duty, activity=prefixed) for duty in schedule.duties)
    result['schedule'] = replace(schedule, duties=duties)
    calls = []
    original = ImageDraw.ImageDraw.text
    def record(self, xy, value, *args, **kwargs):
        calls.append((xy, value))
        return original(self, xy, value, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    with render_dashboard(result, NOW):
        pass
    activity_calls = [xy for xy, value in calls if value == 'Training flight, circuits']
    assert any(xy[0] == 316 for xy in activity_calls)  # featured
    assert any(xy[0] == 337 for xy in activity_calls)  # list
    assert all(duty.activity == prefixed for duty in duties)
    assert result['schedule'].duties == duties
    assert report()['schedule'] == schedule
    assert b'SUMMARY:Training flight\\, circuits' in RAW
    assert not any('2025/10' in value or 'E.1.11' in value for _, value in calls)


@pytest.mark.parametrize('participants,expected', [
    ('', 'Casey Fiction'),
    ('Instructor: Taylor Demo', 'Casey Fiction · Instructor: Taylor Demo'),
    ('Student: Casey Fiction', 'Student: Casey Fiction'),
])
def test_embedded_student_preserved_without_invented_role(participants, expected):
    from plugins.flight_duties.rendering import display_participants
    duty = replace(report()['schedule'].duties[0],
                   activity='2025/11a 🅃 Casey Fiction - E.1.11 Training flight',
                   participants=participants)
    assert display_activity(duty.activity) == 'Training flight'
    assert display_participants(duty) == expected
    assert duty.participants == participants
    assert 'Casey Fiction' in duty.activity


def test_embedded_student_visible_in_featured_and_list(monkeypatch):
    result = report()
    result['schedule'] = replace(result['schedule'], duties=tuple(
        replace(d, activity='2025/10 🅃 Casey Fiction - E.1.11 Training flight', participants='')
        for d in result['schedule'].duties))
    calls = []
    original = ImageDraw.ImageDraw.text
    def record(self, xy, value, *args, **kwargs):
        calls.append((xy, value))
        return original(self, xy, value, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
    with render_dashboard(result, NOW):
        pass
    assert any(xy[0] == 316 and value == 'Casey Fiction' for xy, value in calls)
    assert any(xy[0] == 337 and value == 'Casey Fiction' for xy, value in calls)


def test_unprefixed_participants_unchanged():
    from plugins.flight_duties.rendering import display_participants
    duty = report()['schedule'].duties[0]
    assert display_participants(duty) == duty.participants
