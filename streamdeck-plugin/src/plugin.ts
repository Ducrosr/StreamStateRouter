import streamDeck from "@elgato/streamdeck";
import {
  AutoAction,
  CancelPreviewAction,
  LayoutAction,
  ReapplyAction,
  TogglePauseAction,
  UndoLayoutAction,
} from "./actions/commands";

streamDeck.logger.setLevel("info");
streamDeck.actions.registerAction(new TogglePauseAction());
streamDeck.actions.registerAction(new AutoAction());
streamDeck.actions.registerAction(new ReapplyAction());
streamDeck.actions.registerAction(new LayoutAction());
streamDeck.actions.registerAction(new UndoLayoutAction());
streamDeck.actions.registerAction(new CancelPreviewAction());
streamDeck.connect();
