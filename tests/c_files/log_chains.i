typedef unsigned int base_t;
typedef base_t level1_t;
typedef level1_t level2_t;
typedef int arr10_t[10];
typedef struct Node Node;
struct Node {
    level2_t v;
    arr10_t data;
    Node *next;
};
