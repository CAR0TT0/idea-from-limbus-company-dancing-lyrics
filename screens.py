"""Stable screen selection with a primary-screen fallback during disconnection."""
import json


def screen_key(screen):
    serial = screen.serialNumber()
    return "serial:" + json.dumps([screen.manufacturer(), screen.model(), serial], ensure_ascii=False) if serial else "name:" + screen.name()


def select_screen(screens, primary, preferred):
    if preferred != "primary":
        match = next((screen for screen in screens if screen_key(screen) == preferred), None)
        if match is not None:
            return match
    return primary or (screens[0] if screens else None)
