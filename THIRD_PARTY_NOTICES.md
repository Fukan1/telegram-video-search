# Third-party components

Application source in `src/` is licensed under MIT. Unmodified dependencies retain their own licenses:

| Component | License | Source |
| --- | --- | --- |
| Python | Python Software Foundation | https://www.python.org/downloads/source/ |
| Telethon | MIT | https://github.com/LonamiWebs/Telethon |
| PySide6 Essentials / Shiboken6 / Qt | LGPL v3 option | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| python-socks | Apache 2.0 | https://github.com/romis2012/python-socks |
| pyaes | MIT | https://github.com/ricmoo/pyaes |
| rsa | Apache 2.0 | https://github.com/sybrenstuvel/python-rsa |
| pyasn1 | BSD | https://github.com/pyasn1/pyasn1 |
| qrcode | BSD | https://github.com/lincolnloop/python-qrcode |
| PyInstaller bootloader | GPL with bootloader distribution exception | https://github.com/pyinstaller/pyinstaller |

The application uses dynamically loaded Qt and Python libraries in `_internal/` and does not restrict replacing those libraries or reverse engineering for debugging modifications to LGPL components. The source, dependency list, and PyInstaller specification are provided for rebuilding with compatible replacement libraries.

Relevant license texts are in `licenses/`; the release packaging script also collects notices from the installed dependency versions. Windows and Microsoft runtime components retain their respective licenses. Additional Qt notices: https://doc.qt.io/qt-6/licenses-used-in-qt.html .
