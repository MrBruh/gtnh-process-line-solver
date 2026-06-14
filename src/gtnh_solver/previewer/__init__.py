"""previewer — interactive 3D preview of candidate layouts.

three.js renders the LayoutResult JSON (boxes for machines, polylines for pipes/cables,
labels, congestion heatmap) and supports loading multiple runs so seed-dependent results can
be compared before committing one (this replaces the deferred formal Pareto feature in v1).
v1 can use three.js via CDN with static assets served by the CLI; no npm build required.

TODO(previewer): define the layout->scene mapping; ship a static viewer + a Python emit that
writes the LayoutResult JSON the viewer loads.
"""
