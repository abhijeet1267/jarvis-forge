# Vantage macOS Native App — Build + Package
APP_NAME := Vantage
APP_BUNDLE := $(APP_NAME).app

.PHONY: all debug release app install clean

all: release

debug:
	swift build

release:
	swift build -c release

app: release
	chmod +x assemble_app.sh
	./assemble_app.sh release

app-debug: debug
	chmod +x assemble_app.sh
	./assemble_app.sh debug

# Install through the checked installer, which validates the source bundle,
# backs up the existing install, swaps atomically and verifies the result.
# This target used to run `rm -rf /Applications/Vantage.app` first: a failed or
# partial copy then left the user with no app and no rollback. The installer
# keeps `/Applications/Vantage.app.backup-<timestamp>` until the replacement
# verifies, so an interrupted install is recoverable.
install: app
	../installers/macos/install_app.sh $(CURDIR)/$(APP_BUNDLE)

clean:
	rm -rf $(APP_BUNDLE) $(APP_NAME).iconset
	swift package clean