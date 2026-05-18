"""Perception subsystems that surface non-event snapshot blocks into the prompt.

Unlike Perceptions (which trigger MindLoop iterations), these modules expose
``snapshot()`` methods that the prompt renderer pulls on demand.
"""
