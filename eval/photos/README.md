# Eval photos

Put test photos here with the filenames listed in `../labels.csv` (or edit the CSV to match yours).
Mix real finds (`expected=accept`) with wrong ones (`expected=reject`): a leaf for "acorn", a green leaf for
"red leaf", an indoor shot for "sky", a photo of a screen, a blurry photo, a very dark photo.

Photos in this folder are git-ignored so they stay private. Run:

    python -m eval.run_eval
