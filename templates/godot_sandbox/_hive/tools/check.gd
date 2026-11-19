extends SceneTree
# MultiConnect checker: loads every given resource and reports compile/load errors.
# Usage: godot --headless --path <project> -s res://_hive/tools/check.gd -- file1.gd scenes/x.tscn

func _init() -> void:
	var failed := 0
	for p in OS.get_cmdline_user_args():
		var path: String = p if p.begins_with("res://") else "res://" + p
		if not FileAccess.file_exists(path):
			print("HIVE_MISSING ", path)
			failed += 1
			continue
		var res = ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_IGNORE)
		if res == null:
			print("HIVE_FAIL ", path)
			failed += 1
		elif res is Script and not res.can_instantiate():
			print("HIVE_FAIL ", path)
			failed += 1
		elif res is PackedScene and not res.can_instantiate():
			print("HIVE_FAIL ", path)
			failed += 1
		else:
			print("HIVE_OK ", path)
	print("HIVE_DONE ", failed)
	quit(1 if failed > 0 else 0)
