# Smart Compact Navigation v19

This update reduces the vertical space used by the top navigation.

## Changes

- Navigation groups are collapsed by default.
- Opening one group closes the other groups.
- Group contents open as a compact floating menu instead of pushing the page down.
- Clicking a menu option closes the group menu after navigating.
- Favorites are editable from the UI.
- Each menu item has a star button:
  - filled star means the item is in Favorites
  - empty star means it is not in Favorites
- Favorites are stored in browser localStorage only.
- No database changes are required.

## Storage

Favorites are saved under this localStorage key:

```text
oiapp.smartNav.favorites.v1
```

Clearing browser site data resets favorites back to the default list.
