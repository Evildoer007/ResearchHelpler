# Third-party browser asset

`echarts.min.js` is Apache ECharts 6.0.0, copied from the locally installed
Panel distribution solely to make Research Helper's internal review HTML work
without network access.

- Project: Apache ECharts
- License: Apache License 2.0
- Upstream: https://echarts.apache.org/

It is used only by `render/interactive.py`; the customer-facing one-page PDF
does not depend on this browser asset.
