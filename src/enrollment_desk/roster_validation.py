"""Bounded roster-container checks shared by local and AWS interfaces."""
import io
import zipfile
from .roster_parser import parse_roster

MAX_UPLOAD = 5 * 1024 * 1024
MAX_MEMBERS = 500


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def validate_container(filename, data):
    """Reject oversized/invalid containers before the workflow can consume them."""
    if not 0 < len(data) <= MAX_UPLOAD:
        raise ApiError(400, 'Choose a nonempty roster up to 5 MB.')
    if filename.endswith('.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(i.file_size for i in entries) > 20 * 1024 * 1024:
                    raise ApiError(400, 'Excel file expands beyond the supported limit.')
        except zipfile.BadZipFile:
            raise ApiError(400, 'Invalid Excel file.') from None


def validate_roster(filename, data, **mapping):
    validate_container(filename, data)
    try:
        roster = parse_roster(filename, data, **mapping)
    except Exception:
        raise ApiError(400, 'Cannot read this roster. Upload a valid CSV or XLSX file.') from None
    if not 0 < len(roster.members) <= MAX_MEMBERS:
        raise ApiError(400, f'Upload between 1 and {MAX_MEMBERS} members per roster.')
    if len(roster.model_dump_json().encode()) > 128 * 1024:
        raise ApiError(400, 'Parsed roster is too large for the workflow. Split it into smaller files.')
