import json

def hx_toast(message: str, type: str = "info") -> dict:
    event_data = {
        "show-toast": {
            "message": message,
            "type": type
        }
    }
    # 🟢 FIXED: Changed from 'HX-Trigger' to 'HX-Trigger-After-Swap'
    # This guarantees HTMX waits for the new page to load before popping the toast!
    return {"HX-Trigger-After-Swap": json.dumps(event_data)}