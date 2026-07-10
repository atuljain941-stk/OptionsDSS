# Alert Hub scanner expression autocomplete - v36

This update adds the same Scanner Builder style autocomplete and primitive parameter help to the Alert Hub expression box.

## Behavior

- Type a primitive name such as `RSIDiff90`, `Between`, `TouchStrongCandleLevel`, or `MACDSpreadStable` and the picker suggests matching primitives.
- Type `scan(` and it suggests saved/built-in scanners.
- Press `Shift+Enter` or click **Pick scanner/primitive** to open the full picker.
- Arrow up/down moves through suggestions.
- Enter inserts the selected suggestion.
- Choosing a primitive inserts `PrimitiveName()` and places the cursor inside the parentheses.
- The helper panel stays visible and highlights the current parameter as commas are entered.

The implementation fetches function metadata from `/scanner-builder/api/catalog`, so Alert Hub and Scanner Builder stay in sync.
