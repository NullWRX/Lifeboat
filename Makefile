# Mikoshi install rules. Used by the PKGBUILD; also works for manual installs:
#   sudo make install            (core + GUI into /usr)
#   make DESTDIR=/tmp/pkg install-core
PREFIX  ?= /usr
DESTDIR ?=
PYTHON  ?= python3

LIBDIR   = $(DESTDIR)$(PREFIX)/lib/mikoshi
BINDIR   = $(DESTDIR)$(PREFIX)/bin
SHAREDIR = $(DESTDIR)$(PREFIX)/share
UNITDIR  = $(DESTDIR)$(PREFIX)/lib/systemd/system

.PHONY: all install install-core install-gui uninstall test lint run-gui

all:
	@echo "Nothing to build. Targets: install, install-core, install-gui, test, run-gui"

install: install-core install-gui

install-core:
	install -d $(LIBDIR)/mikoshi/modules
	install -m644 src/mikoshi/*.py $(LIBDIR)/mikoshi/
	install -m644 src/mikoshi/modules/*.py $(LIBDIR)/mikoshi/modules/
	install -m755 src/mikoshi/rsh-local $(LIBDIR)/mikoshi/rsh-local
	install -Dm755 data/bin/mikoshi $(BINDIR)/mikoshi
	install -Dm644 data/systemd/mikoshi@.service $(UNITDIR)/mikoshi@.service
	install -Dm644 data/systemd/mikoshi-plug@.service $(UNITDIR)/mikoshi-plug@.service
	install -Dm644 data/systemd/mikoshi.tmpfiles $(DESTDIR)$(PREFIX)/lib/tmpfiles.d/mikoshi.conf
	install -Dm644 data/pacman/05-mikoshi.hook $(SHAREDIR)/libalpm/hooks/05-mikoshi.hook
	install -Dm644 data/completions/mikoshi.bash $(SHAREDIR)/bash-completion/completions/mikoshi
	install -Dm644 data/completions/_mikoshi $(SHAREDIR)/zsh/site-functions/_mikoshi
	install -Dm644 data/completions/mikoshi.fish $(SHAREDIR)/fish/vendor_completions.d/mikoshi.fish
	install -Dm644 data/man/mikoshi.8 $(SHAREDIR)/man/man8/mikoshi.8
	install -Dm644 data/man/mikoshi.conf.5 $(SHAREDIR)/man/man5/mikoshi.conf.5
	install -Dm644 README.md $(SHAREDIR)/doc/mikoshi/README.md
	$(PYTHON) -m compileall -q -d $(PREFIX)/lib/mikoshi/mikoshi $(LIBDIR)/mikoshi/*.py $(LIBDIR)/mikoshi/modules

install-gui:
	install -d $(LIBDIR)/mikoshi/gui
	install -m644 src/mikoshi/gui/*.py $(LIBDIR)/mikoshi/gui/
	install -Dm755 data/bin/mikoshi-gui $(BINDIR)/mikoshi-gui
	install -Dm644 data/desktop/io.github.nullwrx.Mikoshi.desktop $(SHAREDIR)/applications/io.github.nullwrx.Mikoshi.desktop
	install -Dm644 data/desktop/io.github.nullwrx.Mikoshi.metainfo.xml $(SHAREDIR)/metainfo/io.github.nullwrx.Mikoshi.metainfo.xml
	install -Dm644 data/icons/io.github.nullwrx.Mikoshi.svg $(SHAREDIR)/icons/hicolor/scalable/apps/io.github.nullwrx.Mikoshi.svg
	install -Dm644 data/polkit/io.github.nullwrx.mikoshi.policy $(SHAREDIR)/polkit-1/actions/io.github.nullwrx.mikoshi.policy
	$(PYTHON) -m compileall -q -d $(PREFIX)/lib/mikoshi/mikoshi/gui $(LIBDIR)/mikoshi/gui

uninstall:
	rm -rf $(LIBDIR)
	rm -f $(BINDIR)/mikoshi $(BINDIR)/mikoshi-gui
	rm -f $(UNITDIR)/mikoshi@.service $(UNITDIR)/mikoshi-plug@.service
	rm -f $(DESTDIR)$(PREFIX)/lib/tmpfiles.d/mikoshi.conf $(SHAREDIR)/libalpm/hooks/05-mikoshi.hook
	rm -f $(SHAREDIR)/bash-completion/completions/mikoshi $(SHAREDIR)/zsh/site-functions/_mikoshi
	rm -f $(SHAREDIR)/fish/vendor_completions.d/mikoshi.fish
	rm -f $(SHAREDIR)/man/man8/mikoshi.8 $(SHAREDIR)/man/man5/mikoshi.conf.5
	rm -rf $(SHAREDIR)/doc/mikoshi
	rm -f $(SHAREDIR)/applications/io.github.nullwrx.Mikoshi.desktop
	rm -f $(SHAREDIR)/metainfo/io.github.nullwrx.Mikoshi.metainfo.xml
	rm -f $(SHAREDIR)/icons/hicolor/scalable/apps/io.github.nullwrx.Mikoshi.svg
	rm -f $(SHAREDIR)/polkit-1/actions/io.github.nullwrx.mikoshi.policy

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q tests

# Run the GUI from the source tree (uses pkexec for the helper unless you're root).
run-gui:
	MIKOSHI_DEV=1 PYTHONPATH=src $(PYTHON) -c "from mikoshi.gui.app import main; main()"
