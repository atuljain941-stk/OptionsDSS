# Scanner Builder primitive-column rounding and reordering - V73

## Changes

- Result-column numeric display now defaults to a smart two-decimal view.
  - Existing templates that saved a numeric primitive as `text` are still displayed compactly when the value is numeric.
  - Use `Raw text` only when the unformatted value should be shown exactly as returned.
- Added explicit number display formats:
  - `Number 0 decimals`
  - `Number 1 decimal`
  - `Number 2 decimals`
  - `Number 3 decimals`
  - `Number 4 decimals`
- The Scanner Builder primitive catalog exposes the `Round(expr[, decimals])` function.
  - Examples:
    - `Round(RSIDiff90(90, "1d"), 0)`
    - `Round(RSIDiff90(90, "1d"), 1)`
    - `Round(RelativeStrength(20, "1d"), 2)`
- Added rounded RSIDiff90 examples to the primitive-column catalog.
- Column templates can now be reordered from the Result columns chip list.
  - Use `<` / `>` buttons on each chip.
  - Or drag and drop the chips.
  - Save the template to persist the order.
- Scanner Dashboard uses the same formatting behavior for saved primitive-column templates.

## Notes

- `Integer` and `Number 0 decimals` both display rounded whole numbers.
- `Round(expr, 0)` returns an integer-style value from the scanner expression itself.
- Display formats only affect presentation; they do not change the underlying scan logic.
