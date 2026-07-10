"""OpenUSDConnect usdview integration package.

The PluginContainer lives in ``plugin.py`` so this package's top-level
import stays free of PySide6. Non-Qt callers (the launcher, tests) can
import sibling modules like ``launcher`` and ``connection`` without
triggering usdview's Qt dependency.
"""
