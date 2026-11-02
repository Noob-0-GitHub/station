---
name: browser
description: Open a real browser - click, fill in forms, log in, read dynamic pages
---
# browser

To read the text of a page, web extract is simpler. When you need to press buttons, log in, scroll, or the page needs JavaScript, use the browser command in bash:

```
browser open https://example.com
browser snapshot -i            # list interactive elements, numbered like @e1
browser click @e2
browser fill @e3 "text to type"
browser get text body
browser screenshot ~/shots/page.png   # then look at it with the view tool
browser close
```

Full usage: `browser skills get core --full`

This browser keeps its logins in ~/.browser-profile. It is yours alone.
