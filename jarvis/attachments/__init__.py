"""Attachment processing helpers (text extraction for the pipeline).

Each format gets an honest extractor: it returns real extracted content when it can, and
a truthful failure reason ("unavailable" / "encrypted" / "unreadable") when it cannot —
never a placeholder presented as understood content.
"""