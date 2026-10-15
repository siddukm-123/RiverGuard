Place custom YOLO weights here (e.g. `models/best.pt`, trained on an excavator/JCB dataset) and set on Railway:

    CUSTOM_JCB_MODEL_PATH=models/best.pt

The Dockerfile copies this folder into the image. Until a custom model is present, excavator detection is OFF
and the UI says so. See training/ and README.md. Do not commit weights you are not allowed to redistribute.
