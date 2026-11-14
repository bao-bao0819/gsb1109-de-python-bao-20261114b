struct Node2;

struct Node1 {
    int value;
    struct Node1 *self_link;
    struct Node2 *peer;
};

struct Node2 {
    int value;
    struct Node1 *back;
    struct Node2 *self_link;
};

union Holder {
    int i;
    float f;
};

struct Bits {
    unsigned int a : 3;
    unsigned int b : 5;
    union Holder h;
};

typedef struct {
    int kind;
    union Holder *hptr;
} Envelope;
