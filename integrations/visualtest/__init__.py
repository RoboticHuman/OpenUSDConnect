"""Visual-regression harness: render USD scenes and FLIP-compare against goldens.

Integration-agnostic infrastructure for catching rendered-output regressions
(materials, transforms, cameras, lights, ...). See ``docs/testing-setup.md``.

Import submodules directly (``from integrations.visualtest import render`` then
``render.render(...)``); the heavy ``pxr`` and ``flip_evaluator`` imports are
deferred, so importing a submodule stays cheap and needs the ``visual`` group
only when a render or compare actually runs.
"""
