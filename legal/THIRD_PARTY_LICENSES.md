# Third-party licenses

This file lists the third-party software that is included in or installed by this repository and needs a notice, as the
license of each one requires. For each item it gives the copyright holder as published, the license, how Hokma uses it and the
file with the full license text. The full texts are in legal/licenses/. The notices of the projects whose license is MIT or
Apache-2.0 and that are only named as the origin of an idea are in .notices/THIRD_PARTY.txt, which also carries the notice of
the backtest engine (the one library of this list that the brand rule does not allow to be named outside that folder).

Texts in legal/licenses/:
- LGPL-3.0.txt: GNU Lesser General Public License, version 3. It refers to the GPL-3.0.
- GPL-3.0.txt: GNU General Public License, version 3 (the LGPL-3.0 is an addition to it, so both texts travel together).
- MPL-2.0.txt: Mozilla Public License, version 2.0.
- Apache-2.0.txt: Apache License, version 2.0.

## Libraries under the GNU Lesser General Public License v3 (LGPL-3.0)

Hokma installs each of these as a separate package with uv, never modifies it, never copies a file of it into Hokma and lets the
student replace it with another version of the same package. The code that changes behavior lives in Hokma's own files. Full text:
legal/licenses/LGPL-3.0.txt and legal/licenses/GPL-3.0.txt.

| Package | Version | License | Copyright holder as published | Where Hokma uses it | Source code |
|---|---|---|---|---|---|
| psycopg | 3.3.8 | LGPL-3.0-only | Daniele Varrazzo | backend/hokma/db/postgres.py and db/probe.py: the connection to PostgreSQL | https://psycopg.org/ |
| psycopg-binary | 3.3.8 | LGPL-3.0-only | Daniele Varrazzo | the compiled part of psycopg; its wheel also carries third-party libraries (libpq and encryption libraries) whose own notices are not itemized here | https://psycopg.org/ |
| python-telegram-bot | 22.8 | LGPL-3.0-only (the project is published under GPL-3.0 or LGPL-3.0 at the user's choice; Hokma uses it under the LGPL-3.0) | Leandro Toledo | backend/hokma/telegram/: notices and approvals with buttons | https://github.com/python-telegram-bot/python-telegram-bot |
| portion | 2.6.3 | LGPL-3.0-or-later | Alexandre Decan | it is a dependency of the backtest engine; Hokma's own code does not import it | https://github.com/AlexandreDecan/portion |

The backtest engine is also under the LGPL-3.0-or-later: its notice is in .notices/THIRD_PARTY.txt, section "Backtest engine".

Not modified, installed separately and replaceable by the student: Hokma does not restrict modifying these libraries or debugging
the changes.

## Libraries under the Mozilla Public License 2.0 (MPL-2.0)

Used without modification, installed as separate packages. MPL-2.0 is a license per file: the files of these packages stay under it,
and their source code is available at the address given. Full text: legal/licenses/MPL-2.0.txt.

| Package | Version | License | Copyright holder as published | Source code |
|---|---|---|---|---|
| certifi | 2026.10.23 | MPL-2.0 (contains the Mozilla root certificate bundle) | Kenneth Reitz (author named in the package metadata) | https://github.com/certifi/python-certifi |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | ijl, copyright 2018 - 2026 | https://github.com/ijl/orjson |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | Casper da Costa-Luis 2015-2026 (MPL-2.0 files); Noam Yorav-Raphael 2013 and Google Inc. 2016 (MIT files) | https://tqdm.github.io |

## Data set under the Apache License 2.0

Contract properties, margin series, trading hours and expiration vectors under backend/hokma/contracts/data/ were transcribed as
plain data tables from the public data files of the Lean project, published under the Apache License, Version 2.0.

- Copyright holder as published in the license of that project: Copyright 2014 QuantConnect.
- License: Apache-2.0. Full text: legal/licenses/Apache-2.0.txt and https://www.apache.org/licenses/LICENSE-2.0
- Changes made (Apache-2.0 section 4b): the values were transcribed by a script into Hokma's own CSV files; they were checked against
  exchange specifications and against hand computed expectations in the tests. No source code of that project is included.
- No NOTICE file of that project was found, so there is none to reproduce (section 4d).
- The fee tables under backend/hokma/broker/fees/ were also read from the fee models of that project, which cite the public price
  list of each broker; they are plain data in Hokma's own JSON format and their origin is stated in .notices/THIRD_PARTY.txt.
- Where the data came from: backend/hokma/contracts/data/SOURCES.txt.

## Not yet covered

The complete list of third-party packages with their licenses (the 155 packages of uv.lock, the npm packages of dashboard/ and the Rust
crates of desktop/) is not reproduced here. The permissive ones (MIT, BSD, Apache, ISC, PSF) need only their notice, which each package
carries inside its own installed files. The reading of the licenses of the JavaScript and Rust packages and of the eight packages that
are only installed on Windows, macOS or the browser has not been done yet.
