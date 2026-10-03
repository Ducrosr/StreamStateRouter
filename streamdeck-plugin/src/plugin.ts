import streamDeck from "@elgato/streamdeck";
import {
  AutoAction,
  CancelPreviewAction,
  ConnectionSettingsAction,
  ControlVariableAction,
  LayoutAction,
  MediaNextAction,
  MediaPreviousAction,
  MediaStopAction,
  MediaToggleAction,
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
streamDeck.actions.registerAction(new ControlVariableAction());
streamDeck.actions.registerAction(new MediaToggleAction());
streamDeck.actions.registerAction(new MediaNextAction());
streamDeck.actions.registerAction(new MediaPreviousAction());
streamDeck.actions.registerAction(new MediaStopAction());
streamDeck.actions.registerAction(new ConnectionSettingsAction());
streamDeck.connect();
