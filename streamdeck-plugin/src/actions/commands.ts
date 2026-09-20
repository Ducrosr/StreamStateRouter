import type { KeyDownEvent } from "@elgato/streamdeck";
import streamDeck, { action, SingletonAction } from "@elgato/streamdeck";
import { saveConnectionSettings, ssrClient } from "../ssr-client";

type EmptySettings = Record<string, never>;
type LayoutSettings = { layout?: string; mode?: "apply" | "preview" };
type ConnectionSettings = { port?: number; token?: string };

abstract class CommandAction<T extends object> extends SingletonAction<T> {
  protected async run(ev: KeyDownEvent<T>, fn: () => Promise<unknown>): Promise<void> {
    try {
      await fn();
      await ev.action.showOk();
    } catch (error) {
      streamDeck.logger.error("Commande Stream State Router impossible", error);
      await ev.action.showAlert();
    }
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.toggle-pause" })
export class TogglePauseAction extends CommandAction<EmptySettings> {
  override async onKeyDown(ev: KeyDownEvent<EmptySettings>): Promise<void> {
    await this.run(ev, async () => {
      const status = await ssrClient.status();
      await ssrClient.pause(!Boolean(status.paused));
    });
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.auto" })
export class AutoAction extends CommandAction<EmptySettings> {
  override async onKeyDown(ev: KeyDownEvent<EmptySettings>): Promise<void> {
    await this.run(ev, () => ssrClient.auto());
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.reapply" })
export class ReapplyAction extends CommandAction<EmptySettings> {
  override async onKeyDown(ev: KeyDownEvent<EmptySettings>): Promise<void> {
    await this.run(ev, () => ssrClient.reapply());
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.layout" })
export class LayoutAction extends CommandAction<LayoutSettings> {
  override async onKeyDown(ev: KeyDownEvent<LayoutSettings>): Promise<void> {
    const settings = ev.payload.settings;
    const name = String(settings.layout ?? "").trim();
    if (!name) {
      await ev.action.showAlert();
      return;
    }
    await this.run(ev, () => settings.mode === "preview" ? ssrClient.previewLayout(name) : ssrClient.applyLayout(name));
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.undo-layout" })
export class UndoLayoutAction extends CommandAction<EmptySettings> {
  override async onKeyDown(ev: KeyDownEvent<EmptySettings>): Promise<void> {
    await this.run(ev, () => ssrClient.undoLayout());
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.cancel-preview" })
export class CancelPreviewAction extends CommandAction<EmptySettings> {
  override async onKeyDown(ev: KeyDownEvent<EmptySettings>): Promise<void> {
    await this.run(ev, () => ssrClient.cancelPreview());
  }
}

@action({ UUID: "com.remyducros.streamstaterouter.connection" })
export class ConnectionSettingsAction extends CommandAction<ConnectionSettings> {
  override async onKeyDown(ev: KeyDownEvent<ConnectionSettings>): Promise<void> {
    await this.run(ev, async () => {
      await saveConnectionSettings(ev.payload.settings);
      await ssrClient.status();
    });
  }
}
