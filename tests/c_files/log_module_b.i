typedef unsigned int u32;
typedef unsigned char u8;

struct LinkB {
    int code;
    struct LinkB *next;
};

typedef struct HeaderB {
    u32 magic;
    struct LinkB *link;
    struct HeaderB *owner;
} HeaderB;

typedef struct {
    u32 ts;
    u8 level;
    HeaderB hdr;
    char msg[64];
    u8 flags;
} Payload;

typedef char Status;

struct WrapB {
    Status s;
};
