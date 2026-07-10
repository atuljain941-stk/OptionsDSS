# Smart Compact Navigation v20

Fixes viewport clamping for smart-navigation dropdowns.

## Changes

- Group dropdowns now use fixed positioning and are clamped inside the browser viewport.
- Tools and other wide menus no longer open off the left or right side of the screen.
- Menus remain compact, scrollable, and close behavior is unchanged.
- Favorites remain browser-local via localStorage; no database changes.

## Files changed

- `oiapp/static/style.css`
- `oiapp/static/app.js`
