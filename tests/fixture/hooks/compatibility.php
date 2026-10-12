<?php if (trim(file_get_contents('/opt/installer/incompatible')) === '1') exit(41); if (PHP_MAJOR_VERSION !== 8 || PHP_MINOR_VERSION < 4) exit(1); echo "compatible\n";
