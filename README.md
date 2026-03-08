# KLL – Keyboard Live Light

A small project I made in about 2–3 hours to adapt the color of a **single-zone RGB keyboard** based on the dominant color currently displayed on the screen.

It captures the screen, calculates the dominant color using a simple algorithm, and sends that color to the keyboard through the OpenRGB SDK.

This project uses **OpenRGB**:
https://github.com/CalcProgrammer1/OpenRGB

---

## Requirements

You only need **OpenRGB** and this program.

1. Download and install OpenRGB  
2. Launch OpenRGB and enable the **SDK Server**
3. Run the script / executable

That's it.

---

## Toggle Shortcut (Experimental)

Press: (ctrl+alt+l) to toggle the ambient lighting **on/off**.

Note: The shortcut is experimental and may occasionally require pressing twice.

---

## How It Works

1. The program captures the screen using **mss** about **15 times per second**.
2. The captured frame is analyzed to determine the **dominant color**.
3. The calculated color is sent to **OpenRGB**, which updates the keyboard lighting.

The algorithm ignores **dark pixels** when determining the dominant color to avoid dim scenes affecting the color too much.

Brightness is calculated from the overall frame so the keyboard still reflects darker or brighter scenes appropriately.

Color transitions are smoothed slightly to avoid abrupt lighting changes.

---

## Performance

The program is designed to be lightweight.

On my system (**Intel i7 13th gen**) it uses roughly:

- **Average CPU usage:** ~0.4–0.6%
- **Maximum observed:** ~1.1%

Your results may vary depending on hardware and screen resolution.

---

## Compilation

For testing, I compiled the project using **Nuitka**, which slightly improved performance.

That was the command for it: 

> python.exe -m nuitka --standalone --onefile --windows-console-mode=disable --enable-plugin=numpy --plugin-enable=tk-inter --include-package=mss --include-package=cv2 --output-dir=dist "main(mss).py"

---

## Known Issues

- The toggle shortcut sometimes needs to be pressed more than once to trigger.
- The color detection algorithm is... mysterious.

When I wrote it, **only God and I understood how it worked.  
Now only God knows.**

---

## Contributing

If you have ideas for improvements, optimizations, or bug fixes, feel free to contribute.

My coding skills are far from perfect, so contributions are very welcome.

---

## License

This project is free for personal use, modification, and sharing.

Commercial use requires explicit written permission from the author.

See the **LICENSE** file for details.
