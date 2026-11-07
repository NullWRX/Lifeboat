# Lifeboat install rules. Used by the PKGBUILD; also works for manual installs:
#   sudo make install            (core + GUI into /usr)
#   make DESTDIR=/tmp/pkg install-core
PREFIX  ?= /usr
DESTDIR ?=
PYTHON  ?= python3

LIBDIR   = $(DESTDIR)$(PREFIX)/lib/lifeboat
BINDIR   = $(DESTDIR)$(PREFIX)/bin
SHAREDIR = $(DESTDIR)$(PREFIX)/share
UNITDIR  = $(DESTDIR)$(PREFIX)/lib/systemd/system

.PHONY: all install install-core install-gui uninstall test lint run-gui

all:
	@echo "Nothing to build. Targets: install, install-core, install-gui, test, run-gui"

install: install-core install-gui

install-core:
	install -d $(LIBDIR)/lifeboat/modules
	install -m644 src/lifeboat/*.py $(LIBDIR)/lifeboat/
	install -m644 src/lifeboat/modules/*.py $(LIBDIR)/lifeboat/modules/
	install -m755 src/lifeboat/rsh-local $(LIBDIR)/lifeboat/rsh-local
	install -Dm755 data/bin/lifeboat $(BINDIR)/lifeboat
	install -Dm644 data/systemd/lifeboat@.service $(UNITDIR)/lifeboat@.service
	install -Dm644 data/systemd/lifeboat-plug@.service $(UNITDIR)/lifeboat-plug@.service
	install -Dm644 data/systemd/lifeboat.tmpfiles $(DESTDIR)$(PREFIX)/lib/tmpfiles.d/lifeboat.conf
	install -Dm644 data/pacman/05-lifeboat.hook $(SHAREDIR)/libalpm/hooks/05-lifeboat.hook
	install -Dm644 data/completions/lifeboat.bash $(SHAREDIR)/bash-completion/completions/lifeboat
	install -Dm644 data/completions/_lifeboat $(SHAREDIR)/zsh/site-functions/_lifeboat
	install -Dm644 data/completions/lifeboat.fish $(SHAREDIR)/fish/vendor_completions.d/lifeboat.fish
	install -Dm644 data/man/lifeboat.8 $(SHAREDIR)/man/man8/lifeboat.8
	install -Dm644 data/man/lifeboat.conf.5 $(SHAREDIR)/man/man5/lifeboat.conf.5
	install -Dm644 README.md $(SHAREDIR)/doc/lifeboat/README.md
	$(PYTHON) -m compileall -q -d $(PREFIX)/lib/lifeboat/lifeboat $(LIBDIR)/lifeboat/*.py $(LIBDIR)/lifeboat/modules

install-gui:
	install -d $(LIBDIR)/lifeboat/gui
	install -m644 src/lifeboat/gui/*.py $(LIBDIR)/lifeboat/gui/
	install -Dm755 data/bin/lifeboat-gui $(BINDIR)/lifeboat-gui
	install -Dm644 data/desktop/io.github.nullwrx.Lifeboat.desktop $(SHAREDIR)/applications/io.github.nullwrx.Lifeboat.desktop
	install -Dm644 data/desktop/io.github.nullwrx.Lifeboat.metainfo.xml $(SHAREDIR)/metainfo/io.github.nullwrx.Lifeboat.metainfo.xml
	install -Dm644 data/icons/io.github.nullwrx.Lifeboat.svg $(SHAREDIR)/icons/hicolor/scalable/apps/io.github.nullwrx.Lifeboat.svg
	install -Dm644 data/polkit/io.github.nullwrx.lifeboat.policy $(SHAREDIR)/polkit-1/actions/io.github.nullwrx.lifeboat.policy
	$(PYTHON) -m compileall -q -d $(PREFIX)/lib/lifeboat/lifeboat/gui $(LIBDIR)/lifeboat/gui

uninstall:
	rm -rf $(LIBDIR)
	rm -f $(BINDIR)/lifeboat $(BINDIR)/lifeboat-gui
	rm -f $(UNITDIR)/lifeboat@.service $(UNITDIR)/lifeboat-plug@.service
	rm -f $(DESTDIR)$(PREFIX)/lib/tmpfiles.d/lifeboat.conf $(SHAREDIR)/libalpm/hooks/05-lifeboat.hook
	rm -f $(SHAREDIR)/bash-completion/completions/lifeboat $(SHAREDIR)/zsh/site-functions/_lifeboat
	rm -f $(SHAREDIR)/fish/vendor_completions.d/lifeboat.fish
	rm -f $(SHAREDIR)/man/man8/lifeboat.8 $(SHAREDIR)/man/man5/lifeboat.conf.5
	rm -rf $(SHAREDIR)/doc/lifeboat
	rm -f $(SHAREDIR)/applications/io.github.nullwrx.Lifeboat.desktop
	rm -f $(SHAREDIR)/metainfo/io.github.nullwrx.Lifeboat.metainfo.xml
	rm -f $(SHAREDIR)/icons/hicolor/scalable/apps/io.github.nullwrx.Lifeboat.svg
	rm -f $(SHAREDIR)/polkit-1/actions/io.github.nullwrx.lifeboat.policy

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q tests

# Run the GUI from the source tree (uses pkexec for the helper unless you're root).
run-gui:
	LIFEBOAT_DEV=1 PYTHONPATH=src $(PYTHON) -c "from lifeboat.gui.app import main; main()"
