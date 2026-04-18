from setuptools import setup

APP = ["menubar_app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "LSUIElement": True,
        "CFBundleName": "Meeting Transcriber",
        "CFBundleDisplayName": "Meeting Transcriber",
        "CFBundleIdentifier": "com.meeting-transcriber",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSMicrophoneUsageDescription": (
            "Required to transcribe your microphone during meetings."
        ),
    },
    "packages": ["rumps"],
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
