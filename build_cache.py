import os
import time
import numpy as np
from fastembed import TextEmbedding
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxonomy_project.settings')
django.setup()

from classifier.models import Category

def main():
    cats = list(Category.objects.all())
    print(f"Total categories fetched: {len(cats)}")
    if not cats:
        print("No categories found!")
        return

    print("Initializing FastEmbed with threads=4...")
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", threads=4)
    names = [c.name for c in cats]
    ids = [str(c.id) for c in cats]

    print("Embedding categories in parallel...")
    embeddings = list(model.embed(names, batch_size=256))
    vec_matrix = np.array(embeddings, dtype=np.float32)

    out_path = '/app/category_embeddings.npz'
    np.savez_compressed(out_path, ids=np.array(ids), names=np.array(names), vectors=vec_matrix)
    print(f"SUCCESSFULLY CREATED {out_path}! Vector shape: {vec_matrix.shape}")

if __name__ == '__main__':
    main()
