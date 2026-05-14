"""Phase 9B — Qt Quick scene-graph items.

The four chart surfaces (``HeatmapItem``, ``CvdItem``, ``VolumeProfileItem``,
``CandleItem``) live here.  The first three are **bridge tier**
``QQuickPaintedItem`` wrappers that delegate paint to the matching
``QWidget`` so the existing render logic (and its regression suite) is
reused unchanged.  ``CandleItem`` is a native ``QQuickItem`` that builds a
``QSGGeometryNode`` on the render thread per
``UI_STRATEGY_INTEGRATION_PLAN.md §22.3 / 9B-final``.

Bridge items are tagged ``# TODO Phase 9B-final: migrate to QSGNode`` at the
class level so the bridge tier is easy to find and remove later.
"""
