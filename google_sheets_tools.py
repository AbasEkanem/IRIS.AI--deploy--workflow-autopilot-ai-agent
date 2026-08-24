"""
google_sheets_tools.py
======================
Production-ready Google Sheets API v4 tools for IRIS.AI (Grace Subagent).

Capabilities:
- Create new Google Spreadsheets
- Read spreadsheet metadata & list sheet tabs
- Read cell values formatted as markdown tables
- Update / overwrite cell values in specified ranges
- Append new rows of data to sheets / tables
- Add new sheet tabs to existing spreadsheets
- Clear cell ranges
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Union

from langchain_core.tools import tool

from google_auth import execute_with_retry, get_service
from idempotency import idempotent

_log = logging.getLogger(__name__)


def _svc():
    return get_service("sheets")


def _parse_2d_values(values: Union[list, str, Any]) -> list[list[Any]]:
    """Parse values into a guaranteed 2D list.

    Accepts:
    - 2D list: [["A", "B"], ["C", "D"]]
    - 1D list (single row): ["A", "B", "C"] -> [["A", "B", "C"]]
    - JSON string representation of 1D or 2D list
    """
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
            values = parsed
        except (ValueError, TypeError):
            # Treat single string as a single cell 1x1
            return [[values]]

    if not isinstance(values, (list, tuple)):
        return [[values]]

    if len(values) == 0:
        return []

    # If first element is not a list/tuple, treat entire list as 1 row
    if not isinstance(values[0], (list, tuple)):
        return [list(values)]

    # Convert all inner rows to lists
    return [list(row) if isinstance(row, (list, tuple)) else [row] for row in values]


@tool
@idempotent("create_google_spreadsheet", key_args=["title", "sheet_names"])
def create_google_spreadsheet(title: str, sheet_names: Optional[list[str]] = None) -> str:
    """Create a new Google Spreadsheet.

    Args:
        title: Title/name for the new spreadsheet.
        sheet_names: Optional list of sheet tab names (e.g. ['Summary', 'Q1_Data']).
                     Defaults to a single 'Sheet1' tab if omitted.

    Returns:
        Confirmation string with Spreadsheet ID and direct URL.
    """
    body: dict[str, Any] = {
        "properties": {"title": title}
    }
    if sheet_names and isinstance(sheet_names, list) and len(sheet_names) > 0:
        body["sheets"] = [{"properties": {"title": name}} for name in sheet_names]

    try:
        res = execute_with_retry(lambda: _svc().spreadsheets().create(body=body).execute())
        spreadsheet_id = res["spreadsheetId"]
        spreadsheet_url = res.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")
        sheet_count = len(res.get("sheets", []))
        _log.info("Created Google Spreadsheet '%s' (ID: %s)", title, spreadsheet_id)
        return (
            f"✅ Created Google Spreadsheet: **{title}**\n"
            f"• Spreadsheet ID: `{spreadsheet_id}`\n"
            f"• Direct URL: {spreadsheet_url}\n"
            f"• Sheet Tabs: {sheet_count}"
        )
    except Exception as e:
        _log.error("Failed to create spreadsheet '%s': %s", title, e)
        return f"⚠️ Spreadsheet creation failed: {e}"


@tool
def get_spreadsheet_details(spreadsheet_id: str) -> str:
    """Retrieve metadata and sheet tab details for a Google Spreadsheet.

    Args:
        spreadsheet_id: The ID of the Google Spreadsheet.

    Returns:
        Structured summary with spreadsheet title, URL, and list of sheet tabs with dimensions.
    """
    try:
        res = execute_with_retry(lambda: _svc().spreadsheets().get(spreadsheetId=spreadsheet_id).execute())
        title = res.get("properties", {}).get("title", "Untitled Spreadsheet")
        url = res.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")
        sheets = res.get("sheets", [])

        lines = [
            f"📊 **Spreadsheet Details: {title}**",
            f"• ID: `{spreadsheet_id}`",
            f"• URL: {url}",
            f"• Total Sheet Tabs: {len(sheets)}",
            "",
            "**Sheets / Tabs:**",
        ]
        for s in sheets:
            props = s.get("properties", {})
            sheet_title = props.get("title", "Unknown")
            sheet_id = props.get("sheetId", 0)
            grid = props.get("gridProperties", {})
            rows = grid.get("rowCount", 0)
            cols = grid.get("columnCount", 0)
            lines.append(f"  - **{sheet_title}** (Sheet ID: `{sheet_id}`, Grid: {rows} rows × {cols} cols)")

        return "\n".join(lines)
    except Exception as e:
        _log.error("Failed to get spreadsheet details for '%s': %s", spreadsheet_id, e)
        return f"⚠️ Failed to get spreadsheet details: {e}"


@tool
def read_sheet_values(spreadsheet_id: str, range_name: str = "Sheet1!A1:Z100") -> str:
    """Read cell values from a Google Spreadsheet in A1 notation.

    Args:
        spreadsheet_id: The ID of the Google Spreadsheet.
        range_name: The A1 notation range to read (e.g. 'Sheet1!A1:D10' or 'A1:Z50').

    Returns:
        The extracted cell values formatted as a readable markdown table.
    """
    try:
        res = execute_with_retry(
            lambda: _svc().spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()
        )
        rows = res.get("values", [])
        if not rows:
            return f"ℹ️ No data found in range `{range_name}` for spreadsheet `{spreadsheet_id}`."

        # Format rows into a markdown table
        max_cols = max(len(r) for r in rows)
        padded_rows = [r + [""] * (max_cols - len(r)) for r in rows]

        header = padded_rows[0]
        header_line = "| " + " | ".join(str(c) if str(c).strip() else " " for c in header) + " |"
        sep_line = "| " + " | ".join("---" for _ in header) + " |"
        body_lines = [
            "| " + " | ".join(str(c) if str(c).strip() else " " for c in r) + " |"
            for r in padded_rows[1:]
        ]

        out = [
            f"📋 **Data from `{range_name}`** ({len(rows)} rows):",
            "",
            header_line,
            sep_line,
        ] + body_lines
        return "\n".join(out)
    except Exception as e:
        _log.error("Failed to read sheet values for '%s' range '%s': %s", spreadsheet_id, range_name, e)
        return f"⚠️ Read sheet values failed: {e}"


@tool
def update_sheet_values(
    spreadsheet_id: str,
    range_name: str,
    values: Union[list[list[Any]], list[Any], str],
    value_input_option: str = "USER_ENTERED"
) -> str:
    """Update / write cell values into a specified range of a Google Spreadsheet.

    Args:
        spreadsheet_id: The ID of the Google Spreadsheet.
        range_name: The target A1 range (e.g. 'Sheet1!A1:C3' or 'Sheet1!A1').
        values: 2D list of row values (e.g. [["Name", "Score"], ["Alice", 95]]) or JSON array string.
        value_input_option: How values are parsed. 'USER_ENTERED' (parses formulas/numbers/dates) or 'RAW'.

    Returns:
        Confirmation with number of updated rows, columns, and cells.
    """
    matrix = _parse_2d_values(values)
    if not matrix:
        return "⚠️ No valid values provided to update."

    body = {"values": matrix}
    try:
        res = execute_with_retry(
            lambda: _svc().spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption=value_input_option,
                body=body
            ).execute()
        )
        updated_range = res.get("updatedRange", range_name)
        updated_rows = res.get("updatedRows", len(matrix))
        updated_cols = res.get("updatedColumns", max(len(r) for r in matrix))
        updated_cells = res.get("updatedCells", updated_rows * updated_cols)

        _log.info("Updated '%s' at range '%s'", spreadsheet_id, updated_range)
        return (
            f"✅ Updated range `{updated_range}` in spreadsheet `{spreadsheet_id}`.\n"
            f"• Rows updated: {updated_rows}\n"
            f"• Columns updated: {updated_cols}\n"
            f"• Total cells updated: {updated_cells}"
        )
    except Exception as e:
        _log.error("Failed to update sheet values: %s", e)
        return f"⚠️ Update sheet values failed: {e}"


@tool
@idempotent("append_sheet_values", key_args=["spreadsheet_id", "range_name", "values"])
def append_sheet_values(
    spreadsheet_id: str,
    range_name: str,
    values: Union[list[list[Any]], list[Any], str],
    value_input_option: str = "USER_ENTERED"
) -> str:
    """Append one or more rows of data to the end of a sheet or existing table in a Google Spreadsheet.

    Args:
        spreadsheet_id: The ID of the Google Spreadsheet.
        range_name: The A1 notation range searching for existing table (e.g. 'Sheet1!A1' or 'Sheet1').
        values: 2D list of rows to append (e.g. [["Bob", "Engineering", 88]]) or JSON array string.
        value_input_option: How input data is interpreted. 'USER_ENTERED' (default) or 'RAW'.

    Returns:
        Confirmation with appended range and total rows added.
    """
    matrix = _parse_2d_values(values)
    if not matrix:
        return "⚠️ No valid values provided to append."

    body = {"values": matrix}
    try:
        res = execute_with_retry(
            lambda: _svc().spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption=value_input_option,
                insertDataOption="INSERT_ROWS",
                body=body
            ).execute()
        )
        updates = res.get("updates", {})
        updated_range = updates.get("updatedRange", range_name)
        updated_rows = updates.get("updatedRows", len(matrix))

        _log.info("Appended %d row(s) to '%s' at '%s'", updated_rows, spreadsheet_id, updated_range)
        return (
            f"✅ Appended {updated_rows} row(s) to spreadsheet `{spreadsheet_id}`.\n"
            f"• Appended range: `{updated_range}`"
        )
    except Exception as e:
        _log.error("Failed to append sheet values: %s", e)
        return f"⚠️ Append sheet values failed: {e}"


@tool
def add_sheet_tab(spreadsheet_id: str, title: str) -> str:
    """Add a new sheet tab to an existing Google Spreadsheet.

    Args:
        spreadsheet_id: The ID of the Google Spreadsheet.
        title: Title for the new sheet tab (e.g. 'Quarterly_Metrics').

    Returns:
        Confirmation with the new sheet title and sheetId.
    """
    requests = [{
        "addSheet": {
            "properties": {
                "title": title
            }
        }
    }]
    try:
        res = execute_with_retry(
            lambda: _svc().spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests}
            ).execute()
        )
        reply = res.get("replies", [{}])[0].get("addSheet", {})
        sheet_id = reply.get("properties", {}).get("sheetId", "N/A")
        _log.info("Added sheet tab '%s' (ID: %s) to '%s'", title, sheet_id, spreadsheet_id)
        return f"✅ Added sheet tab '**{title}**' (Sheet ID: `{sheet_id}`) to spreadsheet `{spreadsheet_id}`."
    except Exception as e:
        _log.error("Failed to add sheet tab '%s': %s", title, e)
        return f"⚠️ Add sheet tab failed: {e}"


@tool
def clear_sheet_range(spreadsheet_id: str, range_name: str) -> str:
    """Clear all cell values in a specified range of a Google Spreadsheet.

    Args:
        spreadsheet_id: The ID of the Google Spreadsheet.
        range_name: The A1 notation range to clear (e.g. 'Sheet1!A2:Z100').

    Returns:
        Confirmation that the specified range was cleared.
    """
    try:
        res = execute_with_retry(
            lambda: _svc().spreadsheets().values().clear(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                body={}
            ).execute()
        )
        cleared_range = res.get("clearedRange", range_name)
        _log.info("Cleared range '%s' in '%s'", cleared_range, spreadsheet_id)
        return f"✅ Cleared cell range `{cleared_range}` in spreadsheet `{spreadsheet_id}`."
    except Exception as e:
        _log.error("Failed to clear range '%s': %s", range_name, e)
        return f"⚠️ Clear sheet range failed: {e}"


# ── Exported Google Sheets Tools Suite ────────────────────────────────────────
SHEETS_TOOLS = [
    create_google_spreadsheet,
    get_spreadsheet_details,
    read_sheet_values,
    update_sheet_values,
    append_sheet_values,
    add_sheet_tab,
    clear_sheet_range,
]
