# Package

version       = "0.1.0"
author        = "Navid M"
description   = "A new awesome nimble package"
license       = "GPL-3.0-or-later"
srcDir        = "src"
installExt    = @["nim"]
bin           = @["findit_backend"]


# Dependencies

requires "nim >= 2.2.6"
requires "db_connector >= 0.1.0"
