"""
Export selected bodies from the active design as a single STEP file.
Each body is preserved as a separate solid in the STEP file, so slicers
like Bambu Studio can assign different colors/materials per body.
Runs as an add-in: adds a toolbar button under the Utilities tab.
Persists the last used export path to EXPORT_PATH.txt.
"""

import adsk.core
import adsk.fusion
import traceback
import os
import re

_handlers = []
_app = adsk.core.Application.get()
_ui = _app.userInterface

CMD_ID = 'orcaExportBodiesAsSTL'
CMD_NAME = 'Export Bodies as STEP'
CMD_TOOLTIP = 'Select bodies and export them as a single STEP file (preserves individual bodies for multi-color slicing)'
TOOLBAR_PANEL_ID = 'SolidScriptsAddinsPanel'


def get_script_dir():
    return os.path.dirname(os.path.realpath(__file__))


def read_export_path():
    path_file = os.path.join(get_script_dir(), 'EXPORT_PATH.txt')
    if os.path.exists(path_file):
        with open(path_file, 'r') as f:
            return f.read().strip()
    return os.path.expanduser('~/Desktop')


def save_export_path(path):
    path_file = os.path.join(get_script_dir(), 'EXPORT_PATH.txt')
    with open(path_file, 'w') as f:
        f.write(path + '\n')


def _strip_version(name):
    return re.sub(r'\s*v\s*\d+:\d+|:\d+', '', name).strip()


def collect_all_bodies(design):
    """
    Collect all BRepBodies from the design.
    Returns list of (body, display_label) where body is an occurrence proxy
    (world-space) for occurrence bodies, or a root-level body.
    """
    bodies = []
    root = design.rootComponent

    for i in range(root.bRepBodies.count):
        body = root.bRepBodies.item(i)
        label = f'{body.name}  (Root)'
        bodies.append((body, label))

    all_occs = root.allOccurrences
    for i in range(all_occs.count):
        occ = all_occs.item(i)
        occ_label = _strip_version(occ.name)
        for j in range(occ.bRepBodies.count):
            body = occ.bRepBodies.item(j)
            label = f'{body.name}  ({occ_label})'
            bodies.append((body, label))

    return bodies


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = adsk.core.CommandCreatedEventArgs.cast(args).command
            cmd.isOKButtonVisible = True
            cmd.okButtonText = 'Export'
            cmd.cancelButtonText = 'Cancel'
            inputs = cmd.commandInputs

            design = adsk.fusion.Design.cast(_app.activeProduct)
            if not design:
                inputs.addTextBoxCommandInput('err', '', 'No active Fusion design.', 2, True)
                return

            all_bodies = collect_all_bodies(design)
            if not all_bodies:
                inputs.addTextBoxCommandInput(
                    'noBodies', '', 'No bodies found in this design.', 2, True)
                return

            # Export path
            default_path = read_export_path()
            path_group = inputs.addGroupCommandInput('pathGroup', 'Export Location')
            path_group.isExpanded = True
            path_children = path_group.children
            path_children.addStringValueInput('exportPath', 'Path', default_path)
            path_children.addBoolValueInput('browsePath', 'Browse...', False, '', False)

            # Filename
            doc_name = _app.activeDocument.name
            path_children.addStringValueInput('fileName', 'Filename', f'{doc_name}.step')

            # Selection controls
            select_group = inputs.addGroupCommandInput('selectGroup', 'Quick Select')
            select_group.isExpanded = False
            select_children = select_group.children
            select_children.addBoolValueInput('selectAll', 'Select All', False, '', False)
            select_children.addBoolValueInput('deselectAll', 'Deselect All', False, '', False)
            select_children.addStringValueInput('filterText', 'Filter (contains)', '')
            select_children.addBoolValueInput('applyFilter', 'Apply Filter', False, '', False)

            # Body checkboxes
            body_group = inputs.addGroupCommandInput('bodyGroup', 'Bodies')
            body_group.isExpanded = True
            body_children = body_group.children
            for idx, (body, label) in enumerate(all_bodies):
                body_children.addBoolValueInput(f'body_{idx}', label, True, '', True)

            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            _handlers.append(on_execute)

            on_input_changed = InputChangedHandler()
            cmd.inputChanged.add(on_input_changed)
            _handlers.append(on_input_changed)

        except:
            _app.log(f'CommandCreated failed:\n{traceback.format_exc()}')


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            event_args = adsk.core.InputChangedEventArgs.cast(args)
            changed = event_args.input
            inputs = event_args.firingEvent.sender.commandInputs

            if changed.id == 'browsePath':
                folder_dlg = _ui.createFolderDialog()
                folder_dlg.title = 'Choose Export Folder'
                current = inputs.itemById('exportPath').value
                if os.path.isdir(current):
                    folder_dlg.initialDirectory = current
                result = folder_dlg.showDialog()
                if result == adsk.core.DialogResults.DialogOK:
                    inputs.itemById('exportPath').value = folder_dlg.folder

            elif changed.id == 'selectAll':
                self._set_all_bodies(inputs, True)
            elif changed.id == 'deselectAll':
                self._set_all_bodies(inputs, False)
            elif changed.id == 'applyFilter':
                filter_text = inputs.itemById('filterText').value.strip().lower()
                if not filter_text:
                    self._set_all_bodies(inputs, True)
                    return
                body_group = inputs.itemById('bodyGroup')
                children = body_group.children
                for i in range(children.count):
                    inp = children.item(i)
                    if inp.id.startswith('body_'):
                        inp.value = filter_text in inp.name.lower()

        except:
            _app.log(f'InputChanged failed:\n{traceback.format_exc()}')

    def _set_all_bodies(self, inputs, value):
        body_group = inputs.itemById('bodyGroup')
        children = body_group.children
        for i in range(children.count):
            inp = children.item(i)
            if inp.id.startswith('body_'):
                inp.value = value


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            event_args = adsk.core.CommandEventArgs.cast(args)
            inputs = event_args.command.commandInputs

            design = adsk.fusion.Design.cast(_app.activeProduct)
            export_path = inputs.itemById('exportPath').value.strip()
            file_name = inputs.itemById('fileName').value.strip()

            if not file_name:
                file_name = 'export.step'
            if not file_name.lower().endswith(('.step', '.stp')):
                file_name += '.step'

            if not os.path.isdir(export_path):
                os.makedirs(export_path, exist_ok=True)

            save_export_path(export_path)

            # Re-collect bodies (same order as when dialog was built)
            all_bodies = collect_all_bodies(design)

            # Find selected body indices
            selected_indices = []
            body_group = inputs.itemById('bodyGroup')
            children = body_group.children
            for i in range(children.count):
                inp = children.item(i)
                if inp.id.startswith('body_') and inp.value:
                    idx = int(inp.id.split('_')[1])
                    selected_indices.append(idx)

            if not selected_indices:
                _ui.messageBox('No bodies selected.', CMD_NAME)
                return

            # To export only selected bodies as STEP with each body preserved:
            # 1. Record visibility of ALL bodies
            # 2. Hide everything
            # 3. Show only selected bodies
            # 4. Export root component as STEP
            # 5. Restore original visibility

            # Collect all bodies with their original visibility
            all_body_vis = []
            root = design.rootComponent
            for i in range(root.bRepBodies.count):
                body = root.bRepBodies.item(i)
                all_body_vis.append((body, body.isVisible))
            all_occs = root.allOccurrences
            for i in range(all_occs.count):
                occ = all_occs.item(i)
                for j in range(occ.bRepBodies.count):
                    body = occ.bRepBodies.item(j)
                    all_body_vis.append((body, body.isVisible))

            # Build a set of selected body entityTokens for comparison
            # (BRepBody is unhashable, but entityToken is a unique string)
            selected_tokens = set()
            for idx in selected_indices:
                selected_tokens.add(all_bodies[idx][0].entityToken)

            try:
                # Hide all bodies
                for body, _ in all_body_vis:
                    body.isVisible = False

                # Show only selected
                for body, _ in all_body_vis:
                    if body.entityToken in selected_tokens:
                        body.isVisible = True

                # Export as STEP
                output_file = os.path.join(export_path, file_name)
                export_mgr = design.exportManager
                step_opts = export_mgr.createSTEPExportOptions(output_file, root)
                export_mgr.execute(step_opts)

            finally:
                # Restore original visibility
                for body, was_visible in all_body_vis:
                    try:
                        body.isVisible = was_visible
                    except:
                        pass

            exported_names = [all_bodies[idx][1] for idx in selected_indices]
            _ui.messageBox(
                f'Exported {len(exported_names)} bodies to:\n{output_file}\n\n'
                + '\n'.join(f'  {n}' for n in exported_names),
                CMD_NAME
            )

        except:
            _ui.messageBox(f'Export failed:\n{traceback.format_exc()}', 'Error')


# ---------------------------------------------------------------------------
# Add-in lifecycle
# ---------------------------------------------------------------------------

def run(context):
    try:
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        cmd_def = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_TOOLTIP
        )

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        panel = _ui.allToolbarPanels.itemById(TOOLBAR_PANEL_ID)
        if panel:
            existing = panel.controls.itemById(CMD_ID)
            if not existing:
                panel.controls.addCommand(cmd_def)

        _ui.messageBox(
            f'{CMD_NAME} add-in started.\n\n'
            'Find it under Utilities > Add-Ins panel,\n'
            'or press S and search "Export Bodies".',
            CMD_NAME
        )

    except:
        _ui.messageBox(f'Failed to start add-in:\n{traceback.format_exc()}', 'Error')


def stop(context):
    try:
        panel = _ui.allToolbarPanels.itemById(TOOLBAR_PANEL_ID)
        if panel:
            btn = panel.controls.itemById(CMD_ID)
            if btn:
                btn.deleteMe()

        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

    except:
        _ui.messageBox(f'Failed to stop add-in:\n{traceback.format_exc()}', 'Error')
