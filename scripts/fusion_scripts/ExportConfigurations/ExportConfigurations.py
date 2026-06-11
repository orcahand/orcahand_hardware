"""
Export STL files for each configuration in the active Fusion 360 design.
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

CMD_ID = 'orcaExportConfigurations'
CMD_NAME = 'Export Configurations'
CMD_TOOLTIP = 'Export STL files for selected design configurations'
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


def get_config_rows(design):
    if not design.isConfiguredDesign:
        return []
    table = design.configurationTopTable
    rows = table.rows
    return [(i, rows.item(i).name) for i in range(rows.count)]


def _strip_version(name):
    return re.sub(r'\s*v\s*\d+:\d+|:\d+', '', name).strip()


def _name_similarity(a, b):
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    m, n = len(a), len(b)
    longest = 0
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                longest = max(longest, dp[i][j])
    return longest / max(m, n) if max(m, n) > 0 else 0


def _collect_all_bodies(design):
    bodies = []
    root = design.rootComponent
    for i in range(root.bRepBodies.count):
        body = root.bRepBodies.item(i)
        bodies.append((body, body.name, f'Root: {root.name}'))
    all_occs = root.allOccurrences
    for i in range(all_occs.count):
        occ = all_occs.item(i)
        comp = occ.component
        for j in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(j)
            bodies.append((body, body.name, f'Occ: {_strip_version(occ.name)}'))
    return bodies


def _ask_user_pick_body(config_name, candidates):
    lines = [f'No exact component match for configuration "{config_name}".']
    lines.append('Select a body to export (enter number):\n')
    for i, (body, name, parent, sim) in enumerate(candidates):
        lines.append(f'  {i + 1}. {name}  ({parent})  [{sim:.0%} match]')
    lines.append(f'\n  0. Skip this configuration')
    prompt = '\n'.join(lines)
    ret_val, cancelled = _ui.inputBox(prompt, f'Pick body for: {config_name}', '1')
    if cancelled:
        return None
    try:
        choice = int(ret_val.strip())
    except ValueError:
        return None
    if choice == 0 or choice < 0 or choice > len(candidates):
        return None
    return candidates[choice - 1][0]


def find_matching_geometry(design, config_name):
    """
    Find the best geometry to export for a configuration.

    Primary pattern (e.g. config "I-PP"):
      Occurrence "PP (1):1" → body "PP" — body name is contained in config name.
    """
    root = design.rootComponent
    all_occs = root.allOccurrences

    # Pass 1: body inside an occurrence whose name matches the config
    body_candidates = []
    for i in range(all_occs.count):
        occ = all_occs.item(i)
        comp = occ.component
        for j in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(j)
            bname = body.name.strip()
            if bname and (bname in config_name or config_name in bname or bname == config_name):
                occ_name = _strip_version(occ.name)
                body_candidates.append((body, bname, occ_name))

    if len(body_candidates) == 1:
        return body_candidates[0][0], True

    if len(body_candidates) > 1:
        best = None
        best_sim = -1
        for body, bname, occ_name in body_candidates:
            sim = _name_similarity(config_name, bname)
            if sim > best_sim:
                best_sim = sim
                best = body
        sims = sorted([_name_similarity(config_name, b[1]) for b in body_candidates], reverse=True)
        if best_sim >= 0.5 and (len(sims) == 1 or sims[0] - sims[1] > 0.1):
            return best, True
        scored = [(b, bname, f'Occ: {oname}', _name_similarity(config_name, bname))
                  for b, bname, oname in body_candidates]
        scored.sort(key=lambda x: x[3], reverse=True)
        picked = _ask_user_pick_body(config_name, scored[:10])
        if picked:
            return picked, False
        return None, False

    # Pass 2: occurrence name match
    for i in range(all_occs.count):
        occ = all_occs.item(i)
        bname = _strip_version(occ.name)
        if config_name in bname or bname in config_name:
            comp = occ.component
            if comp.bRepBodies.count == 1:
                return comp.bRepBodies.item(0), True
            return occ, True

    # Pass 3: component name match
    for i in range(all_occs.count):
        occ = all_occs.item(i)
        comp_name = occ.component.name
        if config_name in comp_name or comp_name in config_name:
            comp = occ.component
            if comp.bRepBodies.count == 1:
                return comp.bRepBodies.item(0), True
            return occ, True

    # Pass 4: similarity search across all bodies
    all_bodies = _collect_all_bodies(design)
    scored = []
    for body, name, parent in all_bodies:
        sim = _name_similarity(config_name, name)
        if sim >= 0.3:
            scored.append((body, name, parent, sim))
    scored.sort(key=lambda x: x[3], reverse=True)
    if not scored:
        return None, False
    if len(scored) == 1 or (scored[0][3] >= 0.8 and
            (len(scored) == 1 or scored[0][3] - scored[1][3] > 0.2)):
        return scored[0][0], True
    picked = _ask_user_pick_body(config_name, scored[:10])
    if picked:
        return picked, False
    return None, False


def export_stl(design, geometry, output_path, filename):
    export_mgr = design.exportManager
    filepath = os.path.join(output_path, filename + '.stl')
    stl_opts = export_mgr.createSTLExportOptions(geometry, filepath)
    stl_opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    stl_opts.isBinaryFormat = True
    stl_opts.sendToPrintUtility = False
    export_mgr.execute(stl_opts)
    return filepath


def export_step(design, geometry, output_path, filename):
    export_mgr = design.exportManager
    filepath = os.path.join(output_path, filename + '.step')
    step_opts = export_mgr.createSTEPExportOptions(filepath, geometry)
    export_mgr.execute(step_opts)
    return filepath


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class ExportCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
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

            config_rows = get_config_rows(design)
            if not config_rows:
                inputs.addTextBoxCommandInput(
                    'noConfigs', '',
                    'The active design has no configurations.\n'
                    'Open a configured design and try again.', 3, True)
                return

            # Export path — reads last used path
            default_path = read_export_path()
            path_group = inputs.addGroupCommandInput('pathGroup', 'Export Location')
            path_group.isExpanded = True
            path_children = path_group.children
            path_children.addStringValueInput('exportPath', 'Path', default_path)
            path_children.addBoolValueInput('browsePath', 'Browse...', False, '', False)

            # Export format — unchecked = STL (default), checked = STEP
            inputs.addBoolValueInput('exportStep', 'Export as STEP (instead of STL)', True, '', False)

            # Selection controls
            select_group = inputs.addGroupCommandInput('selectGroup', 'Quick Select')
            select_group.isExpanded = False
            select_children = select_group.children
            select_children.addBoolValueInput('selectAll', 'Select All', False, '', False)
            select_children.addBoolValueInput('deselectAll', 'Deselect All', False, '', False)
            select_children.addStringValueInput('filterText', 'Filter (contains)', '')
            select_children.addBoolValueInput('applyFilter', 'Apply Filter', False, '', False)

            # Configuration checkboxes
            config_group = inputs.addGroupCommandInput('configGroup', 'Configurations')
            config_group.isExpanded = True
            config_children = config_group.children
            for idx, name in config_rows:
                config_children.addBoolValueInput(f'config_{idx}', name, True, '', True)

            on_execute = ExportCommandExecuteHandler()
            cmd.execute.add(on_execute)
            _handlers.append(on_execute)

            on_input_changed = ExportInputChangedHandler()
            cmd.inputChanged.add(on_input_changed)
            _handlers.append(on_input_changed)

        except:
            _app.log(f'CommandCreated failed:\n{traceback.format_exc()}')


class ExportInputChangedHandler(adsk.core.InputChangedEventHandler):
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
                self._set_all_configs(inputs, True)
            elif changed.id == 'deselectAll':
                self._set_all_configs(inputs, False)
            elif changed.id == 'applyFilter':
                filter_text = inputs.itemById('filterText').value.strip().lower()
                if not filter_text:
                    self._set_all_configs(inputs, True)
                    return
                config_group = inputs.itemById('configGroup')
                children = config_group.children
                for i in range(children.count):
                    inp = children.item(i)
                    if inp.id.startswith('config_'):
                        inp.value = filter_text in inp.name.lower()

        except:
            _app.log(f'InputChanged failed:\n{traceback.format_exc()}')

    def _set_all_configs(self, inputs, value):
        config_group = inputs.itemById('configGroup')
        children = config_group.children
        for i in range(children.count):
            inp = children.item(i)
            if inp.id.startswith('config_'):
                inp.value = value


class ExportCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            event_args = adsk.core.CommandEventArgs.cast(args)
            inputs = event_args.command.commandInputs

            design = adsk.fusion.Design.cast(_app.activeProduct)
            table = design.configurationTopTable
            rows = table.rows
            export_path = inputs.itemById('exportPath').value.strip()
            use_step = inputs.itemById('exportStep').value
            do_export = export_step if use_step else export_stl
            ext = 'step' if use_step else 'stl'

            if not os.path.isdir(export_path):
                os.makedirs(export_path, exist_ok=True)

            save_export_path(export_path)

            original_row = table.activeRow

            selected = []
            config_group = inputs.itemById('configGroup')
            children = config_group.children
            for i in range(children.count):
                inp = children.item(i)
                if inp.id.startswith('config_') and inp.value:
                    idx = int(inp.id.split('_')[1])
                    selected.append((idx, inp.name))

            if not selected:
                _ui.messageBox('No configurations selected.', 'Export')
                return

            exported = []
            failed = []

            for idx, config_name in selected:
                try:
                    row = rows.item(idx)
                    row.activate()
                    adsk.doEvents()

                    geometry, was_auto = find_matching_geometry(design, config_name)
                    if geometry is None:
                        failed.append(f'{config_name}: no matching component/body found')
                        continue

                    filepath = do_export(design, geometry, export_path, config_name)
                    exported.append(config_name)
                    print(f'Exported: {filepath}')

                except Exception as e:
                    failed.append(f'{config_name}: {str(e)}')

            if original_row:
                try:
                    original_row.activate()
                    adsk.doEvents()
                except:
                    pass

            msg_parts = [f'Exported {len(exported)} {ext.upper()} files to:\n{export_path}']
            if exported:
                msg_parts.append('\nExported:\n' + '\n'.join(f'  {n}.{ext}' for n in exported))
            if failed:
                msg_parts.append('\nFailed:\n' + '\n'.join(f'  {f}' for f in failed))
            _ui.messageBox('\n'.join(msg_parts), 'Export Complete')

        except:
            _ui.messageBox(f'Export failed:\n{traceback.format_exc()}', 'Error')


# ---------------------------------------------------------------------------
# Add-in lifecycle: run() registers the toolbar button, stop() removes it
# ---------------------------------------------------------------------------

def run(context):
    try:
        # Clean up any leftover definition from a previous session
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        cmd_def = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_TOOLTIP
        )

        on_created = ExportCommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        # Add button to the Utilities > Scripts & Add-Ins panel
        panel = _ui.allToolbarPanels.itemById(TOOLBAR_PANEL_ID)
        if panel:
            existing = panel.controls.itemById(CMD_ID)
            if not existing:
                panel.controls.addCommand(cmd_def)

        _ui.messageBox(
            f'{CMD_NAME} add-in started.\n\n'
            'Find it under Utilities > Add-Ins panel,\n'
            'or use the shortcut: S key, then search "Export Configurations".',
            CMD_NAME
        )

    except:
        _ui.messageBox(f'Failed to start add-in:\n{traceback.format_exc()}', 'Error')


def stop(context):
    try:
        # Remove toolbar button
        panel = _ui.allToolbarPanels.itemById(TOOLBAR_PANEL_ID)
        if panel:
            btn = panel.controls.itemById(CMD_ID)
            if btn:
                btn.deleteMe()

        # Remove command definition
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

    except:
        _ui.messageBox(f'Failed to stop add-in:\n{traceback.format_exc()}', 'Error')
